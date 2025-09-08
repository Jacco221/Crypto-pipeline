#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Moonshot v2 — altijd Top-N watchlist produceren.

- Leest scores_latest.csv (of ander pad).
- Primary filter (min_total/min_rs/min_volume, exclude top-N by mcap, exclude bluechips).
- Als er 0 resultaten zijn, produceert hij toch een watchlist:
  → Top N op Total_% (met dezelfde uitsluitingen van top-rank en bluechips).
- Schrijft altijd zowel CSV als Markdown.
"""

import argparse
import sys
from pathlib import Path
from datetime import datetime, timezone

import pandas as pd
import numpy as np

BLUECHIPS = {
    "BTC","ETH","BNB","SOL","XRP","ADA","DOGE","TON","TRX","AVAX","LINK",
    "LTC","BCH","DOT","MATIC","POL","SHIB","OKB","WBTC","UNI","TIA","NEAR",
    "ETC","APT","ARB","OP","FTM","ATOM","HBAR"
}

def find_col(df: pd.DataFrame, candidates):
    for c in candidates:
        if c in df.columns:
            return c
    # case-insensitive fallback
    lower = {c.lower(): c for c in df.columns}
    for c in candidates:
        if c.lower() in lower:
            return lower[c.lower()]
    return None

def load_scores(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    # normalize symbol
    sym_col = find_col(df, ["symbol", "Symbol"])
    if sym_col is None:
        raise SystemExit("symbol-kolom niet gevonden")
    df.rename(columns={sym_col: "symbol"}, inplace=True)
    df["symbol"] = df["symbol"].astype(str).str.upper()

    # rank (market cap rank)
    rcol = find_col(df, ["rank", "rank#", "market_cap_rank"])
    if rcol is None:
        # als 'rank' ontbreekt, zet zeer hoge rank (valt altijd buiten top-N excluder)
        df["rank"] = 9999.0
    else:
        df.rename(columns={rcol: "rank"}, inplace=True)
        df["rank"] = pd.to_numeric(df["rank"], errors="coerce")

    # scores/price/volume
    for cand, new in [
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
            df.rename(columns={col: new}, inplace=True)

    # types
    for c in ["Total_%","RS_%","TA_%","price","ta_volume"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    return df

def to_markdown(df: pd.DataFrame, n: int, mode: str, args) -> str:
    dt = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = []
    lines.append(f"# 🌙 Moonshot v2 — Top {n} ({mode})")
    lines.append("")
    lines.append(f"_Gegenereerd: {dt}_  ")
    lines.append(f"_Filters: min_total={args.min_total}, min_rs={args.min_rs}, min_volume={args.min_volume}, "
                 f"exclude_top_rank<{args.exclude_top_rank}, exclude_bluechips={args.exclude_bluechips}_")
    lines.append("")
    lines.append("| # | Symbool | Naam | Rank | Total_% | RS_% | TA_% | Prijs | Volume |")
    lines.append("|--:|:-------|:-----|----:|-------:|----:|----:|-----:|------:|")
    for i, row in enumerate(df.itertuples(index=False), 1):
        lines.append(
            f"| {i} | {getattr(row,'symbol','')} | {getattr(row,'name','')} | "
            f"{int(getattr(row,'rank',0)) if pd.notna(getattr(row,'rank',np.nan)) else ''} | "
            f"{getattr(row,'Total_%',np.nan):.2f} | "
            f"{getattr(row,'RS_%',np.nan):.2f} | "
            f"{getattr(row,'TA_%',np.nan):.2f} | "
            f"{getattr(row,'price',np.nan):.6g} | "
            f"{getattr(row,'ta_volume',np.nan):.6g} |"
        )
    lines.append("")
    if mode == "watchlist":
        lines.append("> ⚠️ Geen kandidaten voldeden aan de drempels; dit is een **watchlist** (Top-N op Total_%).")
    return "\n".join(lines)

def main():
    p = argparse.ArgumentParser(description="Moonshot v2 — altijd Top-N watchlist produceren")
    p.add_argument("--scores-csv", required=True, help="Pad naar scores_latest.csv")
    p.add_argument("--scores-json", default=None, help="(optioneel) scores_latest.json")
    p.add_argument("--out-csv", required=True, help="Uitvoer CSV")
    p.add_argument("--out-md", required=True, help="Uitvoer Markdown")
    p.add_argument("--top", type=int, default=10, help="Aantal in de Top-N (default 10)")
    p.add_argument("--min-total", type=float, default=60.0, help="Minimale Total_% drempel")
    p.add_argument("--min-rs", type=float, default=50.0, help="Minimale RS_% drempel")
    p.add_argument("--min-volume", type=float, default=0.0, help="Minimale volume (ta_volume) drempel")
    p.add_argument("--exclude-top-rank", type=int, default=30, help="Exclusief top-N marketcap (default 30)")
    p.add_argument("--exclude-bluechips", action="store_true", help="Bluechips uitsluiten")
    args = p.parse_args()

    scores_path = Path(args.scores_csv)
    out_csv = Path(args.out_csv)
    out_md = Path(args.out_md)
    out_csv.parent.mkdir(parents=True, exist_ok=True)

    df = load_scores(scores_path)

    # Baseline universum: exclude top mcap & bluechips (indien gevraagd)
    universe = df.copy()
    if args.exclude_top_rank is not None:
        universe = universe[(universe["rank"].isna()) | (universe["rank"] > float(args.exclude_top_rank))]
    if args.exclude_bluechips:
        universe = universe[~universe["symbol"].isin(BLUECHIPS)]

    # Primary filter (strenge drempels)
    filt = universe.copy()
    if "Total_%" in filt.columns:
        filt = filt[filt["Total_%"] >= float(args.min_total)]
    if "RS_%" in filt.columns:
        filt = filt[filt["RS_%"] >= float(args.min_rs)]
    if "ta_volume" in filt.columns:
        filt = filt[filt["ta_volume"] >= float(args.min_volume)]

    # Sorteer op Total_% aflopend en pak Top N
    def topn(d):
        if "Total_%" not in d.columns:
            return d.head(args.top)
        return d.sort_values("Total_%", ascending=False).head(args.top)

    mode = "filtered"
    result = topn(filt)

    # Fallback: als er niets door de drempels komt → watchlist Top-N (alleen universe-filters)
    if result.empty:
        mode = "watchlist"
        result = topn(universe)

    # Enrich + metadata
    run_dt = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    result = result.copy()
    result.insert(0, "source_mode", mode)
    result.insert(1, "run_utc", run_dt)

    # Kolommen volgorde netjes maken als ze bestaan
    ordered_cols = [
        "source_mode","run_utc","symbol","name","rank","price",
        "TA_%","RS_%","Macro_%","Total_%","ta_volume"
    ]
    cols = [c for c in ordered_cols if c in result.columns] + [c for c in result.columns if c not in ordered_cols]
    result = result[cols]

    # Write CSV & Markdown
    result.to_csv(out_csv, index=False)
    md = to_markdown(result, args.top, mode, args)
    out_md.write_text(md, encoding="utf-8")

    print(f"✅ Moonshot v2 ({mode}): geschreven {out_csv} en {out_md} ({len(result)} rijen)")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"❌ Fout in moonshot_v2: {e}", file=sys.stderr)
        sys.exit(1)
