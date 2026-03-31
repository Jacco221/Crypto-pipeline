#!/usr/bin/env python3
"""
Annotateert top5_latest.md met Market Regime + Advies o.b.v. BTC trend.
Idempotent: als de header er al staat, wordt die geüpdatet i.p.v. dubbel.
"""
from __future__ import annotations
import argparse
import io
import os
import re
from datetime import datetime
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.market_regime import determine_market_regime


HEADER_RE = re.compile(r"^> Market regime: .*$", re.MULTILINE)
ADVICE_RE = re.compile(r"^> Advies: .*$", re.MULTILINE)

def upsert_header(md: str, regime_line: str, advice_line: str) -> str:
    has_header = bool(HEADER_RE.search(md))
    has_advice = bool(ADVICE_RE.search(md))
    if has_header:
        md = HEADER_RE.sub(regime_line, md)
    if has_advice:
        md = ADVICE_RE.sub(advice_line, md)

    if not (has_header and has_advice):
        # Voeg header + advies bovenaan toe met scheidingslijn
        new_head = f"{regime_line}\n{advice_line}\n\n---\n"
        md = new_head + md
    return md


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("md_path", help="Pad naar top5_latest.md")
    args = ap.parse_args()

    info = determine_market_regime()
    regime = info["regime"]
    asof = info["as_of"]
    last_close = info.get("last_close")
    ma20 = info.get("ma20")
    ma200 = info.get("ma200")

    regime_score = info.get("regime_score", "?")
    regime_line = f"> Market regime: {regime} (score {regime_score}/4, as of {asof}, BTC={last_close}, MA20={ma20}, MA200={ma200})"

    if regime == "RISK_OFF":
        advice_line = "> Advies: STABLECOIN (risk-off; geen nieuwe posities)"
    elif regime == "CAUTIOUS":
        advice_line = "> Advies: HALVE ALLOCATIE (cautious; 50% van normaal)"
    else:
        advice_line = "> Advies: Volg Top-picks (risk-on; volledige allocatie)"

    md_path = os.path.expanduser(args.md_path)
    if not os.path.isfile(md_path):
        raise SystemExit(f"Bestand niet gevonden: {md_path}")

    with io.open(md_path, "r", encoding="utf-8") as f:
        md = f.read()

    new_md = upsert_header(md, regime_line, advice_line)

    with io.open(md_path, "w", encoding="utf-8") as f:
        f.write(new_md)

    print("✅ top5 bijgewerkt met marktregime en advies.")
    print(regime_line)
    print(advice_line)


if __name__ == "__main__":
    main()

