#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json, time, sys
from pathlib import Path
import pandas as pd
import numpy as np
import urllib.request

CG_BASE = "https://api.coingecko.com/api/v3"

def http_get(url):
    for i in range(5):
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                return json.load(r)
        except Exception:
            if i == 4: raise
            time.sleep(1.5 * (i + 1))
    return None

def fetch_markets(vs="usd", n=300):
    per_page = 250
    pages = (n + per_page - 1) // per_page
    rows = []
    for p in range(1, pages + 1):
        url = (f"{CG_BASE}/coins/markets?vs_currency={vs}"
               f"&order=market_cap_desc&per_page={per_page}&page={p}"
               f"&price_change_percentage=1h,24h,7d,30d")
        data = http_get(url)
        if not data:
            break
        rows.extend(data)
    return rows[:n]

def safe(x):
    try: return float(x)
    except: return np.nan

def winsor(s, p=0.01):
    lo, hi = s.quantile(p), s.quantile(1 - p)
    return s.clip(lo, hi)

def compute(df: pd.DataFrame) -> pd.DataFrame:
    df["pc_1d"]  = df["price_change_percentage_24h_in_currency"].apply(safe)
    df["pc_7d"]  = df["price_change_percentage_7d_in_currency"].apply(safe)
    df["pc_30d"] = df["price_change_percentage_30d_in_currency"].apply(safe)
    df["ta_volume"] = df["total_volume"].astype(float)

    # gestandaardiseerde score (simpel & robuust)
    m = 0.5*winsor(df["pc_30d"]) + 0.3*winsor(df["pc_7d"]) + 0.2*winsor(df["pc_1d"])
    m = m.fillna(0.0)
    v = df["ta_volume"]
    v = np.log1p((v - v.min()) / (v.max() - v.min() + 1e-9))

    ta = 100*(0.85*m + 0.15*v)

    rs = df["pc_30d"].rank(pct=True)*100
    med30 = np.nanmedian(df["pc_30d"])

    btc30 = float(df.loc[df["symbol"].str.upper()=="BTC","pc_30d"].fillna(0).values[0]) if (df["symbol"].str.upper()=="BTC").any() else med30
    macro = np.clip(50+25*np.sign(med30)+25*np.sign(btc30),0,100)

    total = 0.5*ta + 0.3*rs + 0.2*macro

    out = pd.DataFrame({
        "symbol": df["symbol"].str.upper(),
        "name": df["name"],
        "rank": df["market_cap_rank"].astype(float),
        "price": df["current_price"].astype(float),
        "pc_1d": df["pc_1d"], "pc_7d": df["pc_7d"], "pc_30d": df["pc_30d"],
        "ta_volume": df["ta_volume"].astype(float),
        "TA_%": ta.round(2), "RS_%": rs.round(2), "Macro_%": round(macro,2),
        "Total_%": total.round(2), "AvgDataAge_h": 0.0, "age_h": 1.0, "ta_funding": 0.0
    })
    return out.sort_values(["Total_%","rank"], ascending=[False, True]).reset_index(drop=True)

def main():
    out_csv = Path("data/reports/scores_latest.csv")
    out_json = Path("data/reports/scores_latest.json")
    out_csv.parent.mkdir(parents=True, exist_ok=True)

    print("🌐 Haal marktdata op van CoinGecko…", file=sys.stderr)
    markets = fetch_markets("usd", 300)
    n = len(markets)
    print(f"✔️  opgehaald: {n} coins", file=sys.stderr)
    if n < 200:
        print("❌ Te weinig coins opgehaald (CoinGecko rate/timeout?). Stop.", file=sys.stderr)
        sys.exit(1)

    df = pd.DataFrame(markets)
    if "market_cap_rank" not in df.columns:
        print("❌ 'market_cap_rank' ontbreekt in response.", file=sys.stderr)
        sys.exit(1)

    scores = compute(df)
    scores.to_csv(out_csv, index=False)
    with out_json.open("w") as f:
        json.dump(scores.to_dict(orient="records"), f)

    print(f"✅ Geschreven: {out_csv} en {out_json}", file=sys.stderr)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"Fout in build_scores: {e}", file=sys.stderr)
        sys.exit(1)
