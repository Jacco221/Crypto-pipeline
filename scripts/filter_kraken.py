#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Filter Kraken — snijdt pipeline-scores op verhandelbaarheid bij Kraken.

Werkwijze
- Haalt actuele assets & pairs op via Kraken Public API.
- Bouwt set van verhandelbare *base assets* met opgegeven quotes (USD, EUR, USDT, USDC, ...).
- Normaliseert symbolen (BTC/XBT, DOGE/XDG, XRP/XXRP, enz.) met alias-mapping.
- Filtert een pipeline `scores_latest.csv` of elk ander CSV met kolom `symbol`.
- Schrijft een CSV + optioneel Markdown-rapport met top-N gesorteerd op `Total_%`.

Voorbeeld
  python3 filter_kraken.py \
    --scores-csv data/reports/scores_latest.csv \
    --out-csv    data/reports/scores_kraken_latest.csv \
    --out-md     data/reports/scores_kraken_latest.md \
    --quotes USD,EUR,USDT,USDC \
    --top 50 --exclude-top-rank 30 --exclude-bluechips

Tip: voeg deze stap toe NA build_scores en (optioneel) NA moonshot_v2 in je pipeline.
"""

from __future__ import annotations
import argparse, json, sys
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Set, Iterable
import urllib.request
import pandas as pd
import numpy as np

KRAKEN_BASE = "https://api.kraken.com/0/public"

# Veelvoorkomende alias map (CG symbol → Kraken asset code)
ALIASES: Dict[str, str] = {
    # majors
    "BTC": "XBT", "WBTC": "XBT", "CBBTC": "XBT", "BTC.B": "XBT",
    "ETH": "ETH", "WETH": "ETH", "STETH": "ETH", "WSTETH": "ETH",
    "USDT": "USDT", "USDC": "USDC", "DAI": "DAI", "TUSD": "TUSD",
    "EUR": "ZEUR", "USD": "ZUSD",
    # L1/L2/large caps
    "SOL": "SOL", "AVAX": "AVAX", "BNB": "BNB", "XRP": "XXRP",
    "ADA": "ADA", "DOGE": "XDG", "TRX": "TRX", "DOT": "DOT",
    "LINK": "LINK", "MATIC": "MATIC", "POL": "MATIC",
    "ATOM": "ATOM", "NEAR": "NEAR", "ARB": "ARB", "OP": "OP",
    "ETC": "XETC", "LTC": "XLTC", "BCH": "BCH",
    # overige populaire tickers
    "INJ": "INJ", "KAS": "KAS", "RUNE": "RUNE", "AR": "AR",
    "SUI": "SUI", "PYTH": "PYTH", "TIA": "TIA", "AAVE": "AAVE",
    "PEPE": "PEPE", "BONK": "BONK", "RNDR": "RENDER", "RENDER": "RENDER",
    # soms komt Kraken met voorvoegsels X/Z; we vangen dat downstream ook af
}

BLUECHIPS = {
    "BTC","ETH","BNB","SOL","XRP","ADA","DOGE","TON","TRX","AVAX","LINK",
    "LTC","BCH","DOT","MATIC","POL","SHIB","OKB","WBTC","UNI","TIA","NEAR",
    "ETC","APT","ARB","OP","FTM","ATOM","HBAR"
}


def http_get(url: str, timeout: int = 30):
    for i in range(5):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                data = json.load(r)
                if data.get("error"):
                    raise RuntimeError(f"Kraken API error: {data['error']}")
                return data["result"]
        except Exception as e:
            if i == 4:
                raise
    return None


def fetch_kraken_universe(quotes: Iterable[str]) -> Set[str]:
    """Geef set van verhandelbare *asset codes* (base) die een pair hebben met opgegeven quotes."""
    quotes = {q.upper() for q in quotes}
    # Map quotes naar Kraken codes
    QUOTE_ALIAS = {"USD": "ZUSD", "EUR": "ZEUR", "USDT": "USDT", "USDC": "USDC"}
    kr_quotes = {QUOTE_ALIAS.get(q, q) for q in quotes}

    assets = http_get(f"{KRAKEN_BASE}/Assets") or {}
    assetpairs = http_get(f"{KRAKEN_BASE}/AssetPairs") or {}

    # Zet van geldige base assets die een pair hebben met gewenste quote
    tradable: Set[str] = set()
    for pair_name, meta in assetpairs.items():
        base = meta.get("base", "").upper()
        quote = meta.get("quote", "").upper()
        # Normalize Kraken's X/Z prefixes voor legacy assets
        base_norm = base.replace("X", "X").replace("Z", "Z")
        if quote in kr_quotes:
            tradable.add(base_norm)

    # Voeg ook synoniemen zonder X/Z toe (XBT→BTC etc.) zodat matching makkelijker wordt
    expanded: Set[str] = set()
    for a in tradable:
        expanded.add(a)
        if a.startswith("X") and len(a) > 3:
            expanded.add(a[1:])  # XETH -> ETH, XLTC->LTC
        if a.startswith("Z") and len(a) > 3:
            expanded.add(a[1:])
        if a == "XBT":
            expanded.add("BTC")
    return expanded


def normalize_symbol(sym: str) -> str:
    s = str(sym).upper().strip()
    # simpele schoonmaak
    s = s.replace("-PERP", "").replace(".B", "")
    return s


def map_to_kraken_code(sym: str) -> Set[str]:
    s = normalize_symbol(sym)
    out = {s}
    if s in ALIASES:
        out.add(ALIASES[s])
    # generieke varianten
    if s == "BTC":
        out.update({"XBT"})
    if s == "DOGE":
        out.update({"XDG"})
    if s == "XRP":
        out.update({"XXRP", "XRP"})
    if s == "ETC":
        out.update({"XETC"})
    if s == "LTC":
        out.update({"XLTC"})
    return out


def make_markdown(df: pd.DataFrame, n: int, args) -> str:
    dt = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = []
    lines.append(f"# Kraken-filter — Top {n} verhandelbare coins")
    lines.append("")
    lines.append(f"_Gegenereerd: {dt}_  ")
    lines.append(f"_Quotes: {args.quotes}  |  exclude_top_rank<{args.exclude_top_rank}  |  exclude_bluechips={args.exclude_bluechips}_")
    lines.append("")

    # we gebruiken dict-rijen zodat kolomnamen met '%' gewoon werken
    cols_all = ["symbol","name","rank","price","TA_%","RS_%","Macro_%","Total_%","ta_volume"]
    cols = [c for c in cols_all if c in df.columns]

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
                try:
                    vals.append("" if pd.isna(v) else f"{int(v)}")
                except Exception:
                    vals.append(str(v))
            else:
                vals.append("" if v is None else str(v))
        lines.append("| " + str(i) + " | " + " | ".join(vals) + " |")
    lines.append("")
    return "\n".join(lines)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Filter pipeline scores op Kraken-verhandelbaarheid")
    p.add_argument("--scores-csv", required=True, help="Pad naar scores CSV (bv. data/reports/scores_latest.csv)")
    p.add_argument("--out-csv", required=True, help="Uitvoer CSV (bv. data/reports/scores_kraken_latest.csv)")
    p.add_argument("--out-md", default=None, help="Optioneel: Markdown rapport pad")
    p.add_argument("--quotes", default="USD,EUR,USDT,USDC", help="Komma-lijst van quote currencies")
    p.add_argument("--top", type=int, default=50, help="Aantal rijen in output (na sortering op Total_%)")
    p.add_argument("--exclude-top-rank", type=int, default=None, help="Exclusief top-N marketcap (bv. 30)")
    p.add_argument("--exclude-bluechips", action="store_true", help="Bluechips uitsluiten")
    args = p.parse_args(argv)

    scores_path = Path(args.scores_csv)
    out_csv = Path(args.out_csv)
    out_md = Path(args.out_md) if args.out_md else None

    if not scores_path.exists():
        print(f"❌ scores CSV niet gevonden: {scores_path}", file=sys.stderr)
        return 2

    df = pd.read_csv(scores_path)
    if "symbol" not in df.columns:
        print("❌ Kolom 'symbol' ontbreekt in scores CSV", file=sys.stderr)
        return 2

    # Fetch kraken tradable universe
    quotes = [q.strip() for q in args.quotes.split(",") if q.strip()]
    try:
        k_universe = fetch_kraken_universe(quotes)
    except Exception as e:
        print(f"❌ Kraken API ophalen mislukte: {e}", file=sys.stderr)
        return 3

    # Map ieder score-symbool naar mogelijke Kraken codes en filter
    def is_tradable(sym: str) -> bool:
        for code in map_to_kraken_code(str(sym)):
            if code.upper() in k_universe:
                return True
        return False

    dff = df.copy()
    dff["symbol"] = dff["symbol"].astype(str).str.upper()

    # Universe-restricties (optioneel)
    if args.exclude_top_rank is not None and "rank" in dff.columns:
        dff = dff[(dff["rank"].isna()) | (dff["rank"].astype(float) > float(args.exclude_top_rank))]
    if args.exclude_bluechips:
        dff = dff[~dff["symbol"].isin(BLUECHIPS)]

    # Hou alleen verhandelbaar op Kraken met gewenste quotes
    mask = dff["symbol"].apply(is_tradable)
    dff = dff[mask]

    # Sorteer en top-N
    sort_col = "Total_%" if "Total_%" in dff.columns else None
    if sort_col:
        dff = dff.sort_values(sort_col, ascending=False)
    if args.top and len(dff) > args.top:
        dff = dff.head(args.top)

    out_csv.parent.mkdir(parents=True, exist_ok=True)
    dff.to_csv(out_csv, index=False)

    if out_md is not None:
        md = make_markdown(dff, len(dff), args)
        out_md.write_text(md, encoding="utf-8")

    print(f"✅ Kraken-filter geschreven: {out_csv} ({len(dff)} rijen)" + (f" + {out_md}" if out_md else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

