#!/usr/bin/env python3
import argparse, json, sys
from pathlib import Path
import pandas as pd

def load_top5(top5_csv: Path) -> pd.DataFrame:
    df = pd.read_csv(top5_csv)
    cols = [c.lower() for c in df.columns]
    df.columns = [c.lower() for c in df.columns]
    # Accepteer zowel 'score' als 'total_%' als score-kolom
    if "score" not in df.columns and "total_%" in df.columns:
        df["score"] = df["total_%"]
    if "symbol" not in df.columns:
        raise ValueError("top5 CSV mist kolom 'symbol'")
    if "score" not in df.columns:
        raise ValueError("top5 CSV mist kolom 'score' of 'total_%'")
    return df.sort_values("score", ascending=False).reset_index(drop=True)

def main():
    ap = argparse.ArgumentParser(description="Bepaal allocatie o.b.v. score-kloof (diversificatie-guard).")
    ap.add_argument("--top5", default="data/reports/top5_latest.csv", type=Path)
    ap.add_argument("--out",  default="data/reports/allocation_latest.json", type=Path)
    ap.add_argument("--gap",  type=float, default=2.0, help="Drempel in score-punten: onder deze gap -> diversify.")
    ap.add_argument("--split",type=float, default=0.5, help="Verdeling naar #1 bij diversify (0.5=50/50; 0.6=60/40).")
    ap.add_argument("--append-md", action="store_true", help="Schrijf een korte regel naar top5_latest.md.")
    ap.add_argument("--md-file", default="data/reports/top5_latest.md", type=Path)
    args = ap.parse_args()

    df = load_top5(args.top5)
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
