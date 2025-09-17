#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Moonshot v2 — met optionele Kraken-only filter.

- Leest scores_latest.csv (of ander pad).
- Primary filter (min_total/min_rs/min_volume, exclude top-N by mcap, exclude bluechips).
- Optioneel: beperk universum tot assets die verhandelbaar op Kraken zijn via --kraken-only.
- Als er 0 resultaten zijn, produceert hij toch een watchlist (Top-N op Total_%).
- Schrijft CSV + Markdown.

Voorbeeld:
  python3 scripts/moonshot_v2.py \
    --scores-csv data/reports/scores_latest.csv \
    --out-csv   data/reports/moonshots_kraken_latest.csv \
    --out-md    data/reports/moonshots_kraken_latest.md \
    --top 10 --exclude-top-rank 30 --exclude-bluechips \
    --kraken-only --quotes USD,EUR,USDT,USDC
"""

import argparse
import sys, json, urllib.request
from pathlib import Path
from datetime import datetime, timezone

import pandas as pd
import numpy as np

BLUECHIPS = {
    "BTC","ETH","BNB","SOL","XRP","ADA","DOGE","TON","TRX","AVAX","LINK",
    "LTC","BCH","DOT","MATIC","POL","SHIB","OKB","WBTC","UNI","TIA","NEAR",
    "ETC","APT","ARB","OP","FTM","ATOM","HBAR"
}

# ===== Kraken helpers =====
KRAKEN_BASE = "https://api.kraken.com/0/public"
ALIASES = {
    # majors
    "BTC":"XBT","WBTC":"XBT","CBBTC":"XBT","BTC.B":"XBT",
    "ETH":"ETH","WETH":"ETH","STETH":"ETH","WSTETH":"ETH",
    "USDT":"USDT","USDC":"USDC","DAI":"DAI","TUSD":"TUSD",
    "EUR":"ZEUR","USD":"ZUSD",
    # L1/L2/large caps
    "SOL":"SOL","AVAX":"AVAX","BNB":"BNB","XRP":"XXRP",
    "ADA":"ADA","DOGE":"XDG","TRX":"TRX","DOT":"DOT",
    "LINK":"LINK","MATIC":"MATIC","POL":"MATIC",
    "ATOM":"ATOM","NEAR":"NEAR","ARB":"ARB","OP":"OP",
    "ETC":"XETC","LTC":"XLTC","BCH":"BCH",
    # populaire midcaps
    "INJ":"INJ","KAS":"KAS","RUNE":"RUNE","AR":"AR",
    "SUI":"SUI","PYTH":"PYTH","TIA":"TIA","AAVE":"AAVE",
    "PEPE":"PEPE","BONK":"BONK","RNDR":"RENDER","RENDER":"RENDER",
}
QUOTE_ALIAS = {"USD":"ZUSD","EUR":"ZEUR","USDT":"USDT","USDC":"USDC"}

def http_get(url: str, timeout: int = 30):
    for i in range(5):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                data = json.load(r)
                if isinstance(data, dict) and data.get("error"):
                    raise RuntimeError(f"Kraken API error: {data['error']}")
                return data.get("result", data)
        except Exception:
            if i == 4:
                raise
    return None

def fetch_kraken_universe(quotes):
    quotes = {q.upper() for q in quotes}
    kr_quotes = {QUOTE_ALIAS.get(q, q) for q in quotes}
    assetpairs = http_get(f"{KRAKEN_BASE}/AssetPairs") or {}
    tradable = set()
    for _, meta in assetpairs.items():
        base = str(meta.get("base","")).upper()
        quote = str(meta.get("quote","")).upper()
        if quote in kr_quotes:
            tradable.add(base)
    expanded = set()
    for a in tradable:
        expanded.add(a)
        if a.startswith("X") and len(a) > 3: expanded.add(a[1:])
        if a.startswith("Z") and len(a) > 3: expanded.add(a[1:])
        if a == "XBT": expanded.add("BTC")
    return expanded

def normalize_symbol(s: str) -> str:
    return str(s).upper().strip().replace("-PERP","").replace(".B","")

def kraken_codes_for(sym: str):
    s = normalize_symbol(sym)
    outs = {s}
    if s in ALIASES: outs.add(ALIASES[s])
    if s == "BTC": outs.add("XBT")
    if s == "DOGE": outs.add("XDG")
    if s == "XRP": outs.update({"XXRP","XRP"})
    if s == "ETC": outs.add("XETC")
    if s == "LTC": outs.add("XLTC")
    return {x.upper() for x in outs}
# ===== end Kraken helpers =====

def find_col(df: pd.DataFrame, candidates):
    for c in candidates:
        if c in df.columns: return c
    lower = {c.lower(): c for c in df.columns}
    for c in candidates:
        if c.lower() in lower: return lower[c.lower()]
    return None

def load_scores(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    # symbol
    sym_col = find_col(df, ["symbol","Symbol"])
    if sym_col is None:
        raise SystemExit("symbol-kolom niet gevonden")
    df.rename(columns={sym_col:"symbol"}, inplace=True)
    df["symbol"] = df["symbol"].astype(str).str.upper()
    # rank
    rcol = find_col(df, ["rank","rank#","market_cap_rank"])
    if rcol is None:
        df["rank"] = 9999.0
    else:
        df.rename(columns={rcol:"rank"}, inplace=True)
        df["rank"] = pd.to_numeric(df["rank"], errors="coerce")
    # overige kolommen normaliseren
    for cand,new in [
        (["Total_%","total","TOTAL_%"], "Total_%"),
        (["RS_%","rs","RS"], "RS_%"),
        (["TA_%","ta","TA"], "TA_%"),
        (["price","Price","current_price"], "price"),
        (["ta_volume","total_volume","volume"], "ta_volume"),
        (["name","Name"], "name"),
        (["Macro_%","macro"], "Macro_%"),
    ]:
        col = find_col(df, cand)
        if col is not None:
            df.rename(columns={col:new}, inplace=True)
    for c in ["Total_%","RS_%","TA_%","price","ta_volume"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df

def apply_kraken_filter(df: pd.DataFrame, quotes):
    try:
        universe = fetch_kraken_universe(quotes)
    except Exception as e:
        print(f"❌ Kraken API ophalen mislukte: {e}", file=sys.stderr)
        sys.exit(3)
    def tradable(sym: str) -> bool:
        for code in kraken_codes_for(sym):
            if code in universe: return True
        return False
    mask = df["symbol"].apply(tradable)
    return df[mask]

def to_markdown(df: pd.DataFrame, n: int, mode: str, args) -> str:
    dt = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    label = " (Kraken-only)" if args.kraken_only else ""
    lines = []
    lines.append(f"# 🌙 Moonshot v2 — Top {n} ({mode}){label}")
    lines.append("")
    extra = f" | kr_quotes={args.quotes}" if args.kraken_only else ""
    lines.append(f"_Gegenereerd: {dt}_  ")
    lines.append(f"_Filters: min_total={args.min_total}, min_rs={args.min_rs}, min_volume={args.min_volume}, "
                 f"exclude_top_rank<{args.exclude_top_rank}, exclude_bluechips={args.exclude_bluechips}{extra}_")
    lines.append("")
    cols = [c for c in ["symbol","name","rank","price","TA_%","RS_%","Macro_%","Total_%","ta_volume"] if c in df.columns]
    header = "| # | " + " | ".join("Symbool" if c=="symbol" else c for c in cols) + " |"
    sep    = "|--:|" + "|".join(["---:" for _ in cols]) + "|"
    lines.append(header)
    lines.append(sep)
    for i, row in enumerate(df.to_dict(orient="records"), 1):
        vals = []
        for c in cols:
            v = row.get(c)
            if isinstance(v, float) and c in ("TA_%","RS_%","Macro_%","Total_%"):
                vals.append(f"{v:.2f}")
            elif isinstance(v, float) and c == "price":
                vals.append(f"{v:.6g}")
            elif c == "rank":
                try: vals.append("" if pd.isna(v) else f"{int(v)}")
                except Exception: vals.append(str(v))
            else:
                vals.append("" if v is None else str(v))
        lines.append("| " + str(i) + " | " + " | ".join(vals) + " |")
    lines.append("")
    if mode == "watchlist":
        lines.append("> ⚠️ Geen kandidaten voldeden aan de drempels; dit is een **watchlist** (Top-N op Total_%).")
    return "\n".join(lines)

def main():
    p = argparse.ArgumentParser(description="Moonshot v2 — Top-N, met optioneel Kraken-only universum")
    p.add_argument("--scores-csv", required=True, help="Pad naar scores_latest.csv of moonshots_v2_latest.csv")
    p.add_argument("--scores-json", default=None, help="(optioneel) scores_latest.json")
    p.add_argument("--out-csv", required=True, help="Uitvoer CSV")
    p.add_argument("--out-md", required=True, help="Uitvoer Markdown")
    p.add_argument("--top", type=int, default=10, help="Aantal in de Top-N (default 10)")
    p.add_argument("--min-total", type=float, default=60.0, help="Minimale Total_% drempel")
    p.add_argument("--min-rs", type=float, default=50.0, help="Minimale RS_% drempel")
    p.add_argument("--min-volume", type=float, default=0.0, help="Minimale volume (ta_volume) drempel")
    p.add_argument("--exclude-top-rank", type=int, default=30, help="Exclusief top-N marketcap (default 30)")
    p.add_argument("--exclude-bluechips", action="store_true", help="Bluechips uitsluiten")
    p.add_argument("--kraken-only", action="store_true", help="Beperk universum tot Kraken-verhandelbaar")
    p.add_argument("--quotes", default="USD,EUR,USDT,USDC", help="Komma-lijst quotes (Kraken)")
    args = p.parse_args()

    scores_path = Path(args.scores_csv)
    out_csv = Path(args.out_csv)
    out_md = Path(args.out_md)
    out_csv.parent.mkdir(parents=True, exist_ok=True)

    df = load_scores(scores_path)

    # Universe-filters
    universe = df.copy()
    if args.exclude_top_rank is not None:
        universe = universe[(universe["rank"].isna()) | (universe["rank"] > float(args.exclude_top_rank))]
    if args.exclude_bluechips:
        universe = universe[~universe["symbol"].isin(BLUECHIPS)]

    # Optioneel Kraken-only
    if args.kraken_only:
        quotes = [q.strip() for q in args.quotes.split(",") if q.strip()]
        universe = apply_kraken_filter(universe, quotes)

    # Primary drempels
    filt = universe.copy()
    if "Total_%" in filt.columns:  filt = filt[filt["Total_%"]  >= float(args.min_total)]
    if "RS_%"    in filt.columns:  filt = filt[filt["RS_%"]     >= float(args.min_rs)]
    if "ta_volume" in filt.columns:filt = filt[filt["ta_volume"] >= float(args.min_volume)]

    def topn(d):
        if "Total_%" not in d.columns: return d.head(args.top)
        return d.sort_values("Total_%", ascending=False).head(args.top)

    mode = "filtered"
    result = topn(filt)
    if result.empty:
        mode = "watchlist"
        result = topn(universe)

    run_dt = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    result = result.copy()
    result.insert(0, "source_mode", mode)
    result.insert(1, "run_utc", run_dt)

    ordered_cols = ["source_mode","run_utc","symbol","name","rank","price",
                    "TA_%","RS_%","Macro_%","Total_%","ta_volume"]
    cols = [c for c in ordered_cols if c in result.columns] + [c for c in result.columns if c not in ordered_cols]
    result = result[cols]

    result.to_csv(out_csv, index=False)
    md = to_markdown(result, args.top, mode, args)
    out_md.write_text(md, encoding="utf-8")
    print(f"✅ Moonshot v2{' (Kraken-only)' if args.kraken_only else ''} {mode}: geschreven {out_csv} en {out_md} ({len(result)} rijen)")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"❌ Fout in moonshot_v2: {e}", file=sys.stderr)
        sys.exit(1)

