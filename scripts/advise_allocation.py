#!/usr/bin/env python3
"""
advise_allocation.py — Bepaal allocatie o.b.v. score-kloof (diversificatie-guard).

Leest rankings UITSLUITEND uit scores_latest.csv (de single source of truth).
top5_latest.csv wordt alleen nog als fallback gebruikt als scores_latest.csv
niet beschikbaar is.
"""
import argparse, json, sys
from pathlib import Path
import pandas as pd

def load_scores(scores_csv: Path, top_n: int = 5) -> pd.DataFrame:
    """
    Laad rankings uit scores_latest.csv (primaire bron).
    Accepteer zowel 'score' als 'total_%' als score-kolom.
    Retourneert de top N rijen, gesorteerd op score (desc).
    """
    df = pd.read_csv(scores_csv)
    df.columns = [c.lower() for c in df.columns]
    # Accepteer zowel 'score' als 'total_%' als score-kolom
    if "score" not in df.columns and "total_%" in df.columns:
        df["score"] = df["total_%"]
    if "symbol" not in df.columns:
        raise ValueError(f"{scores_csv} mist kolom 'symbol'")
    if "score" not in df.columns:
        raise ValueError(f"{scores_csv} mist kolom 'score' of 'total_%'")
    # scores_latest.csv is al gesorteerd door run.py, maar sort opnieuw voor zekerheid
    return df.sort_values("score", ascending=False).head(top_n).reset_index(drop=True)

def main():
    ap = argparse.ArgumentParser(description="Bepaal allocatie o.b.v. score-kloof (diversificatie-guard).")
    ap.add_argument("--scores", default="data/reports/scores_latest.csv", type=Path,
                    help="Primaire bron: scores_latest.csv (single source of truth)")
    ap.add_argument("--top5", default="data/reports/top5_latest.csv", type=Path,
                    help="Fallback als --scores niet bestaat (verouderd; gebruik --scores)")
    ap.add_argument("--out",  default="data/reports/allocation_latest.json", type=Path)
    ap.add_argument("--gap",  type=float, default=2.0, help="Drempel in score-punten: onder deze gap -> diversify.")
    ap.add_argument("--split",type=float, default=0.5, help="Verdeling naar #1 bij diversify (0.5=50/50; 0.6=60/40).")
    ap.add_argument("--append-md", action="store_true", help="Schrijf een korte regel naar top5_latest.md.")
    ap.add_argument("--md-file", default="data/reports/top5_latest.md", type=Path)
    args = ap.parse_args()

    # Gebruik scores_latest.csv als primaire bron; top5_latest.csv als fallback
    if args.scores.exists():
        df = load_scores(args.scores)
        print(f"[ALLOCATION] Bron: {args.scores} ({len(df)} rijen geladen)", file=sys.stderr)
    elif args.top5.exists():
        print(f"[ALLOCATION] ⚠️  {args.scores} niet gevonden; fallback naar {args.top5}", file=sys.stderr)
        df = load_scores(args.top5)
    else:
        print(f"[ALLOCATION] ❌  Noch {args.scores} noch {args.top5} gevonden.", file=sys.stderr)
        sys.exit(1)

    gap = None
    if len(df) < 2:
        print("⚠️  Minder dan 2 coins in top5; ga 100% in #1.", file=sys.stderr)
        alloc = {df.loc[0,"symbol"]: 1.0}
        decision = "SINGLE"
    else:
        s1, s2 = float(df.loc[0,"score"]), float(df.loc[1,"score"])
        c1, c2 = df.loc[0,"symbol"], df.loc[1,"symbol"]
        gap = s1 - s2
        if gap < args.gap:
            split1 = round(args.split, 4)
            split2 = round(1.0 - args.split, 4)
            alloc = {c1: split1, c2: split2}
            decision = "DIVERSIFY"
        else:
            alloc = {c1: 1.0}
            decision = "SINGLE"

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w") as f:
        json.dump({"decision": decision, "gap": gap if len(df)>=2 else None, "gap_threshold": args.gap,
                   "allocation": alloc, "top": df.head(5).to_dict(orient="records")}, f, indent=2)
    # Log een duidelijke regel
    if decision == "DIVERSIFY":
        k = list(alloc.keys())
        print(f"[ALLOCATION] DIVERSIFY: {k[0]}={alloc[k[0]]*100:.0f}%  +  {k[1]}={alloc[k[1]]*100:.0f}%  (gap={gap:.2f} < {args.gap})")
    else:
        k = list(alloc.keys())[0]
        print(f"[ALLOCATION] SINGLE: {k}=100%  (gap={gap:.2f} ≥ {args.gap})" if len(df)>=2 else f"[ALLOCATION] SINGLE: {k}=100%")

    if args.append_md:
        try:
            line = ""
            if decision == "DIVERSIFY":
                ks = list(alloc.keys())
                line = f"> Allocatie: DIVERSIFY → {ks[0]} {alloc[ks[0]]*100:.0f}% + {ks[1]} {alloc[ks[1]]*100:.0f}% (gap {gap:.2f} < {args.gap})\n"
            else:
                k = list(alloc.keys())[0]
                line = f"> Allocatie: SINGLE → {k} 100%\n"
            if args.md_file.exists():
                with open(args.md_file, "a") as f:
                    f.write("\n" + line)
        except Exception as e:
            print(f"⚠️  Kon niet naar MD schrijven: {e}", file=sys.stderr)

if __name__ == "__main__":
    main()
