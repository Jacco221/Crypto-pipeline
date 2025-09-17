#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Build Top-5 CSV (symbol,Total_%) from scores_latest.csv.

- Normalise kolommen (symbol, Total_%).
- Optionele filters: exclude top-30 marketcap en bluechips.
- Output exact: symbol,Total_% (matching advise_allocation.py).
"""

import argparse
from pathlib import Path
import pandas as pd

BLUECHIPS = {
    "BTC","ETH","BNB","SOL","XRP","ADA","DOGE","TON","TRX","AVAX","LINK",
    "LTC","BCH","DOT","MATIC","POL","SHIB","OKB","WBTC","UNI","TIA","NEAR",
    "ETC","APT","ARB","OP","FTM","ATOM","HBAR"
}

def pick(df: pd.DataFrame, cands):
    for c in cands:
        if c in df.columns:
            return c
    lower = {c.lower(): c for c in df.columns}
    for c in cands:
        if c.lower() in lower:
            return lower[c.lower()]
    raise SystemExit(f"Kolom niet gevonden (probeerde: {cands}) in {list(df.columns)}")

def main():
    ap = argparse.ArgumentParser(description="Maak top5_latest.csv vanuit scores_latest.csv")
    ap.add_argument("--scores-csv", default="data/reports/scores_latest.csv")
    ap.add_argument("--out-csv",    default="data/reports/top5_latest.csv")
    ap.add_argument("--exclude-top-rank", type=int, default=30)
    ap.add_argument("--exclude-bluechips", action="store_true")
    ap.add_argument("--top", type=int, default=5)
    args = ap.parse_args()

    src = Path(args.scores_csv); out = Path(args.out_csv)
    out.parent.mkdir(parents=True, exist_ok=True)

    df = pd.read_csv(src)

    # normaliseer kolomnamen
    sym = pick(df, ["symbol","Symbol"])
    tot = pick(df, ["Total_%","TOTAL_%","total","score"])
    df = df.rename(columns={sym:"symbol", tot:"Total_%"})
    df["symbol"] = df["symbol"].astype(str).str.upper()
    df["Total_%"] = pd.to_numeric(df["Total_%"], errors="coerce")

    # filters
    if "rank" in df.columns and args.exclude_top_rank is not None:
        df["rank"] = pd.to_numeric(df["rank"], errors="coerce")
        df = df[(df["rank"].isna()) | (df["rank"] > float(args.exclude_top_rank))]
    if args.exclude_bluechips:
        df = df[~df["symbol"].isin(BLUECHIPS)]

    out_df = df.sort_values("Total_%", ascending=False).head(args.top)[["symbol","Total_%"]]
    out_df["Total_%"] = out_df["Total_%"].round(2)
    out_df.to_csv(out, index=False)
    print(f"✅ geschreven: {out.resolve()}  ({len(out_df)} regels)")

if __name__ == "__main__":
    main()
