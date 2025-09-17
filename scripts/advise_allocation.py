#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
advise_allocation.py
--------------------
Bepaalt een eenvoudige allocatie op basis van de Top-5 CSV en schrijft het resultaat
naar JSON. Optioneel wordt er een korte samenvatting toegevoegd aan het MD-rapport.

Logica (simpel en duidelijk):
- Neem de top-2 rijen uit de Top-5 op basis van Total_% (aflopend).
- Als het verschil (#1 - #2) < gap_threshold  => DIVERSIFY 50% / 50%.
- Anders                                      => SINGLE   100% in #1.

Argumenten:
  --top5         Pad naar top5_latest.csv
  --out          Pad naar output JSON
  --gap-threshold Drempel in punten (default 2.0)
  --append-md    Voeg een regel toe aan Markdown-rapport
  --md-file      Pad naar top5_latest.md (verplicht als --append-md is gezet)
"""

from __future__ import annotations
import argparse, json, sys
from pathlib import Path
from datetime import datetime, timezone
import pandas as pd
import numpy as np

def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def load_top5(csv_path: Path) -> pd.DataFrame:
    if not csv_path.exists():
        print(f"❌ top5 CSV bestaat niet: {csv_path}", file=sys.stderr)
        sys.exit(2)
    df = pd.read_csv(csv_path)
    # normaliseer kolomnamen die we gebruiken
    def find_col(cands):
        for c in df.columns:
            if c in cands: return c
        lower = {c.lower(): c for c in df.columns}
        for c in cands:
            if c.lower() in lower: return lower[c.lower()]
        return None
    sym = find_col(["symbol","Symbool"])
    tot = find_col(["Total_%","Total","TOTAL_%"])
    name = find_col(["name","Naam"])
    if sym is None or tot is None:
        print("❌ Vereiste kolommen ontbreken (symbol/Total_%).", file=sys.stderr)
        sys.exit(2)
    df.rename(columns={sym:"symbol", tot:"Total_%"}, inplace=True)
    if name and name != "name":
        df.rename(columns={name:"name"}, inplace=True)
    df["symbol"] = df["symbol"].astype(str).str.upper()
    df["Total_%"] = pd.to_numeric(df["Total_%"], errors="coerce")
    df = df.sort_values("Total_%", ascending=False).reset_index(drop=True)
    return df

def decide_allocation(df: pd.DataFrame, gap_threshold: float = 2.0):
    if df.empty:
        return {"mode":"CASH","weights":{}, "reason":"Lege top5."}
    # pak top 2
    top = df.head(2).copy()
    s1 = top.iloc[0].get("symbol","?")
    t1 = float(top.iloc[0].get("Total_%", np.nan))
    n1 = top.iloc[0].get("name","")
    if len(top) == 1 or np.isnan(top.iloc[1].get("Total_%", np.nan)):
        return {"mode":"SINGLE","weights":{s1:1.0},"gap":None,
                "symbols":[s1], "names":[n1], "scores":[t1],
                "reason":"Slechts één geldige kandidaat."}
    s2 = top.iloc[1].get("symbol","?")
    t2 = float(top.iloc[1].get("Total_%", np.nan))
    n2 = top.iloc[1].get("name","")

    gap = t1 - t2
    if gap < gap_threshold:
        return {"mode":"DIVERSIFY","weights":{s1:0.5, s2:0.5}, "gap":gap,
                "symbols":[s1,s2], "names":[n1,n2], "scores":[t1,t2],
                "reason":f"klein verschil (gap={gap:.2f} < {gap_threshold:.1f})"}
    else:
        return {"mode":"SINGLE","weights":{s1:1.0}, "gap":gap,
                "symbols":[s1,s2], "names":[n1,n2], "scores":[t1,t2],
                "reason":f"duidelijk voordeel voor #1 (gap={gap:.2f} ≥ {gap_threshold:.1f})"}

def append_to_md(md_path: Path, allocation: dict, gap_threshold: float):
    """Voegt een korte Allocatie-sectie toe of vervangt de bestaande regel direct onder '### Allocatie'."""
    if not md_path.exists():
        print(f"⚠️ MD-bestand niet gevonden (sla overslaan): {md_path}", file=sys.stderr)
        return
    md = md_path.read_text(encoding="utf-8")
    lines = md.splitlines()
    # zorg dat er een kopje is
    anchor = None
    for i, L in enumerate(lines):
        if L.strip().lower().startswith("### allocatie"):
            anchor = i
            break
    if anchor is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("### Allocatie")
        anchor = len(lines) - 1

    # bouw nieuwe regel
    mode = allocation.get("mode")
    gap = allocation.get("gap")
    if mode == "DIVERSIFY":
        s1, s2 = allocation["symbols"][:2]
        note = f"ALLOCATION | DIVERSIFY: {s1}=50% + {s2}=50% (gap={gap:.2f} < {gap_threshold:.1f})."
    elif mode == "SINGLE":
        s1 = allocation["symbols"][0]
        if gap is None:
            note = f"ALLOCATION | SINGLE: {s1}=100% (slechts één kandidaat)."
        else:
            note = f"ALLOCATION | SINGLE: {s1}=100% (gap={gap:.2f} ≥ {gap_threshold:.1f})."
    else:
        note = "ALLOCATION | CASH: geen positie (geen geldige kandidaten)."

    # vervang/insert direct onder de header
    j = anchor + 1
    # verwijder bestaande bullet/regel(s) tot lege regel
    while j < len(lines) and lines[j].strip() and not lines[j].startswith("### "):
        # overschrijf slechts één informatieregel
        del lines[j]
    lines.insert(j, f"- {note}")
    md_path.write_text("\n".join(lines) + ("\n" if not md.endswith("\n") else ""), encoding="utf-8")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top5", required=True, help="Pad naar top5_latest.csv")
    ap.add_argument("--out", required=True, help="Pad naar allocation_latest.json")
    ap.add_argument("--gap-threshold", type=float, default=2.0, help="drempel in punten tussen #1 en #2")
    ap.add_argument("--append-md", action="store_true", help="voeg samenvatting toe aan MD")
    ap.add_argument("--md-file", default="data/reports/top5_latest.md", help="MD-bestand (vereist bij --append-md)")
    args = ap.parse_args()

    top5_path = Path(args.top5)
    out_path = Path(args.out)
    md_path = Path(args.md_file)

    df = load_top5(top5_path)
    decision = decide_allocation(df, gap_threshold=float(args.gap_threshold))

    payload = {
        "generated_utc": utcnow_iso(),
        "gap_threshold": float(args.gap_threshold),
        **decision
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"✅ Allocatie opgeslagen: {out_path}")

    if args.append_md:
        append_to_md(md_path, decision, float(args.gap_threshold))
        print(f"📝 Allocatie toegevoegd aan MD: {md_path}")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"❌ Fout in advise_allocation.py: {e}", file=sys.stderr)
        sys.exit(1)

