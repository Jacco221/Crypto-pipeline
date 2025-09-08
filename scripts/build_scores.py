#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys, time, json, math
from pathlib import Path
import pandas as pd
import numpy as np
import urllib.request

CG_BASE = "https://api.coingecko.com/api/v3"

def http_get(url):
    """GET met kleine retry-backoff en korte timeout."""
    for i in range(5):
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                return json.load(r)
        except Exception:
            if i == 4:
                raise
            time.sleep(1.5*(i+1))
    return None

def fetch_markets(vs="usd", n=300):
    """Haal ±n coins op in batches van 250, sortering = market_cap_desc."""
    per_page = 250
    pages = math.ceil(n/per_page)
    rows = []
    for p in range(1, pages+1):
        url = (
            f"{CG_BASE}/coins/markets"
            f"?vs_currency={vs}"
            f"&order=market_cap_desc"
            f"&per_page={per_page}"
            f"&page={p}"
            f"&price_change_percentage=1h,24h,7d,30d"
        )
        data = http_get(url)
        if not data:
            break
        rows.extend(data)
    return rows[:n]

def safe(x):
    try:
        return float(x)
    except Exception:
        return np.nan

def winsor(s, p=0.01):
    lo, hi = s.quantile(p), s.quantile(1-p)
    return s.clip(lo, hi)

def compute(df: pd.DataFrame) -> pd.DataFrame:
    # pak de benodigde velden / normaliseer namen
    df["pc_1d"]  = df.get("price_change_percentage_24h_in_currency", np.nan).apply(safe)
    df["pc_7d"]  = df.get("price_change_percentage_7d_in_currency",  np.nan).apply(safe)
    df["pc_30d"] = df.get("price_change_percentage_30d_in_currency", np.nan).apply(safe)
    df["vol"]    = df.get("total_volume", np.nan).astype(float)

    # robust: market_cap_rank kan None zijn -> NaN, en heet NIET 'rank'
    mcr = df.get("market_cap_rank")
    if mcr is None:
        df["market_cap_rank"] = np.nan
    df["market_cap_rank"] = df["market_cap_rank"].astype(float)

    # winsor + zscore op performance-vensters en volume
    for col in ["pc_1d","pc_7d","pc_30d"]:
        m = winsor(df[col].astype(float).fillna(0.0))
        m = (m - m.min()) / (m.max() - m.min() + 1e-9)  # schaal 0..1
        df[col+"_z"] = (m - m.mean())/(m.std() + 1e-9)

    v = np.log(df["vol"].fillna(0.0) + 1.0)
    v = (v - v.min())/(v.max() - v.min() + 1e-9)
    df["vol_z"] = (v - v.mean())/(v.std() + 1e-9)

    # simpele samengestelde score
    # (deze wegingen hielden in eerdere versie goed stand)
    score = 0.5*df["pc_30d_z"] + 0.3*df["pc_7d_z"] + 0.2*df["pc_1d_z"] + 0.15*df["vol_z"]
    score = score.replace([np.inf,-np.inf], np.nan).fillna(score.mean())
    df["Total_%"] = (score - score.min())/(score.max() - score.min() + 1e-9) * 100.0

    # BTC-bias iets dempen op 30d (optioneel; klein effect)
    is_btc = (df["symbol"].str.upper() == "BTC")
    df.loc[is_btc, "Total_%"] = np.clip(df.loc[is_btc, "Total_%"] - 2.0, 0, 100)

    out = pd.DataFrame({
        "symbol": df["symbol"].str.upper(),
        "name":   df["name"],
        "rank":   df["market_cap_rank"].astype(float),     # <-- hier gebruiken we market_cap_rank
        "price":  df["current_price"].astype(float),
        "ta_volume": df["vol"].astype(float),
        "pc_1d%":  df["pc_1d"].round(2),
        "pc_7d%":  df["pc_7d"].round(2),
        "pc_30d%": df["pc_30d"].round(2),
        "Total_%": df["Total_%"].round(2)
    })

    # sorteer voor consistentie (hoogste score eerst)
    out = out.sort_values(["Total_%"], ascending=[False]).reset_index(drop=True)
    return out

def main():
    out_csv = Path("data/reports/scores_latest.csv")
    out_json = Path("data/reports/scores_latest.json")
    out_csv.parent.mkdir(parents=True, exist_ok=True)

    try:
        mkts = fetch_markets(vs="usd", n=300)
    except Exception as e:
        print(f"Fout in build_scores (fetch): {e}", file=sys.stderr)
        sys.exit(1)

    if not mkts:
        print("Fout in build_scores: geen data van CoinGecko", file=sys.stderr)
        sys.exit(1)

    df = pd.DataFrame(mkts)
    try:
        out = compute(df)
    except Exception as e:
        print(f"Fout in build_scores (compute): {e}", file=sys.stderr)
        # handige hint: laat beschikbare kolommen zien
        print(f"Kolommen ontvangen: {list(df.columns)}", file=sys.stderr)
        sys.exit(1)

    try:
        out.to_csv(out_csv, index=False)
        out.to_json(out_json, orient="records")
        print(f"✅ Geschreven: {out_csv} ({len(out)} rijen)")
        print(f"✅ Geschreven: {out_json}")
    except Exception as e:
        print(f"Fout in build_scores (write): {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
