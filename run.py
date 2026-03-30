#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run.py — rapporten bouwen met verbeterde scoring pipeline.

Scoring: TA (35%) + Momentum/RS (25%) + Macro (25%) + reservering liquiditeit (15%)
Macro: DXY + Fear&Greed + BTC Dominance
RS: Continu (sigmoid) over 7d + 30d
Regime: RISK_ON / CAUTIOUS / RISK_OFF (4-punten systeem)

Output (in --reports-dir):
- scores_latest.csv / scores_latest.json
- latest.csv / latest.json
- top5_latest.csv / top5_latest.md

Gebruik:
  python3 run.py --limit 50 --reports-dir data/reports \
                 [--trade-threshold 0.05] [--current-coin LINK] [--fee-bps 52]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Optional, Tuple, List

try:
    import pandas as pd
except Exception:
    print("Pandas is vereist. Installeer met: pip install pandas", file=sys.stderr)
    raise


# =========================================================
# General helpers
# =========================================================

def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)

def now_iso() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")

def to_pct(x: float) -> str:
    return f"{x*100:.1f}%"

def write_csv_json(df: pd.DataFrame, csv_path: Path, json_path: Path) -> None:
    df.to_csv(csv_path, index=False)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(json.loads(df.to_json(orient="records")), f, ensure_ascii=False, indent=2)

def read_scores_from_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    # Verwachte kolommen: symbol, score. Mogelijke extra: volume_24h, volatility, returns…
    if "symbol" not in df.columns:
        for alt in ("ticker", "coin", "asset"):
            if alt in df.columns:
                df = df.rename(columns={alt: "symbol"})
                break
    if "symbol" not in df.columns:
        raise ValueError("scores CSV mist kolom 'symbol'")

    # Normaliseer score naar 0..1 als nodig
    if "score" not in df.columns:
        raise ValueError("scores CSV mist kolom 'score'")
    if df["score"].max() > 1.0:
        df["score"] = df["score"] / 100.0

    return df


# =========================================================
# Try to compute scores via internal modules; else fallback to existing file
# =========================================================

def compute_scores_via_internal_modules(limit: int) -> Optional[pd.DataFrame]:
    """
    Probeer jouw bestaande modules:
      - src/pipeline.build_scores(limit) -> DataFrame
      - src/scoring.get_scores(limit) -> DataFrame
    Verwacht minimaal: symbol, score.
    """
    # 1) src.pipeline.build_scores
    try:
        from src.pipeline import build_scores  # type: ignore
        df = build_scores(limit=limit)
        if isinstance(df, pd.DataFrame) and "symbol" in df.columns and "score" in df.columns:
            return df
    except Exception:
        pass

    # 2) src.scoring.get_scores
    try:
        from src.scoring import get_scores  # type: ignore
        df = get_scores(limit=limit)
        if isinstance(df, pd.DataFrame) and "symbol" in df.columns and "score" in df.columns:
            return df
    except Exception:
        pass

    return None


def compute_or_load_scores(limit: int, reports_dir: Path) -> pd.DataFrame:
    df = compute_scores_via_internal_modules(limit=limit)
    if df is not None and len(df):
        # Neem niet meer dan limit
        return df.sort_values("score", ascending=False).head(limit).reset_index(drop=True)

    fallback = reports_dir / "scores_latest.csv"
    if not fallback.exists():
        raise FileNotFoundError(
            f"Geen scores beschikbaar. Kon niet berekenen via src/… en '{fallback}' bestaat niet."
        )
    df = read_scores_from_csv(fallback).sort_values("score", ascending=False).head(limit).reset_index(drop=True)
    return df


# =========================================================
# RS vs BTC calculator (robust with fallbacks)
# =========================================================

# Mogelijke kolomnamen voor rendementen in jouw data:
RET_CANDIDATES: List[str] = [
    "ret_24h", "return_24h", "price_change_24h",
    "ret_7d", "return_7d",
    "ret_30d", "return_30d",
]

def first_existing_col(df: pd.DataFrame, names: List[str]) -> Optional[str]:
    for n in names:
        if n in df.columns:
            return n
    return None

def add_rs_vs_btc(df: pd.DataFrame) -> pd.DataFrame:
    """
    Voeg kolom 'rs_vs_btc' toe: rendement(coin) - rendement(BTC) (op 24h/7d/30d, wat beschikbaar is).
    Als geen enkele rendements-kolom beschikbaar is, dan voeg niets toe (laat de tie-break naar fallback gaan).
    """
    if "symbol" not in df.columns:
        return df

    ret_col = first_existing_col(df, RET_CANDIDATES)
    if not ret_col:
        # Geen rendementsdata -> geen rs_vs_btc
        return df

    d = df.copy()
    # Pak BTC-rendement
    btc_rows = d[d["symbol"].str.upper() == "BTC"]
    if btc_rows.empty:
        # Als WBTC aanwezig is, gebruik die als BTC-proxy
        btc_rows = d[d["symbol"].str.upper() == "WBTC"]
    if btc_rows.empty:
        return d  # geen BTC -> geen RS

    try:
        btc_ret = float(btc_rows.iloc[0][ret_col])
    except Exception:
        return d

    # Voor alle coins: RS = ret_coin - ret_btc
    def _rs(x):
        try:
            return float(x) - btc_ret
        except Exception:
            return float("nan")

    d["rs_vs_btc"] = d[ret_col].apply(_rs)
    return d


# =========================================================
# Ranking met tie-breakers
# =========================================================

def rank_with_tiebreakers(df: pd.DataFrame) -> pd.DataFrame:
    """
    Sorteer op:
      1) score (desc)
      2) rs_vs_btc (desc)    — als beschikbaar
      3) volume_24h (desc)   — als beschikbaar
      4) volatility (asc)    — als beschikbaar
      5) symbol (asc)        — stabiele fallback
    """
    d = add_rs_vs_btc(df)

    # Bouw sorteer-sleutels dynamisch op
    sort_cols: List[str] = ["score"]
    ascending: List[bool] = [False]

    if "rs_vs_btc" in d.columns:
        sort_cols.append("rs_vs_btc")
        ascending.append(False)

    if "volume_24h" in d.columns:
        sort_cols.append("volume_24h")
        ascending.append(False)

    if "volatility" in d.columns:
        sort_cols.append("volatility")
        ascending.append(True)

    # Altijd een stabiele laatste sleutel:
    sort_cols.append("symbol")
    ascending.append(True)

    d_sorted = d.sort_values(sort_cols, ascending=ascending, kind="mergesort").reset_index(drop=True)
    return d_sorted


# =========================================================
# Decision with trade threshold / fees
# =========================================================

def compute_decision(
    df_scores: pd.DataFrame,
    current_coin: Optional[str],
    trade_threshold: float,
    fee_bps: int,
) -> Tuple[str, str, float, float]:
    """
    Retourneert: (advies, target_symbol, best_score, diff)
    advies = "HOLD" of "SWITCH"
    diff = best_score - current_score
    """
    best = df_scores.iloc[0]
    best_symbol = str(best["symbol"])
    best_score = float(best["score"])

    fees_frac = max(fee_bps / 10_000.0, 0.0)
    effective_threshold = max(trade_threshold, fees_frac)

    if not current_coin:
        # Geen positie -> we beslissen o.b.v. drempel (marktfilter wordt elders toegepast in workflow)
        if best_score >= effective_threshold:
            return ("SWITCH", best_symbol, best_score, best_score)
        else:
            return ("HOLD", "", best_score, best_score)

    cur_rows = df_scores[df_scores["symbol"].str.upper() == current_coin.upper()]
    current_score = float(cur_rows.iloc[0]["score"]) if len(cur_rows) else 0.0

    diff = best_score - current_score
    if best_symbol.upper() == current_coin.upper():
        return ("HOLD", best_symbol, best_score, 0.0)

    if diff >= effective_threshold:
        return ("SWITCH", best_symbol, best_score, diff)
    else:
        return ("HOLD", current_coin, best_score, diff)


# =========================================================
# Writers
# =========================================================

def write_top5_csv_md(
    df_scores: pd.DataFrame,
    reports_dir: Path,
    decision: Tuple[str, str, float, float],
    trade_threshold: float,
    fee_bps: int,
) -> None:
    top5 = df_scores.head(5).copy()
    (reports_dir / "top5_latest.csv").write_text(top5.to_csv(index=False), encoding="utf-8")

    advice, target, best_score, diff = decision
    lines = []
    lines.append(f"# Top 5 (gegenereerd: {now_iso()})\n")
    lines.append(top5.to_markdown(index=False))
    lines.append("\n## Tie-break uitleg")
    tips = []
    if "rs_vs_btc" in top5.columns:
        tips.append("- **RS vs BTC**: hogere RS → voorrang bij gelijke score")
    if "volume_24h" in top5.columns:
        tips.append("- **Volume**: hoger volume → voorrang")
    if "volatility" in top5.columns:
        tips.append("- **Volatiliteit**: lagere vol → voorrang")
    if tips:
        lines.extend(tips)
    else:
        lines.append("- Alleen alfabetische fallback toegepast\n")

    lines.append("\n## Trade-beleid (kosten-/drempelbescherming)")
    lines.append(f"- Drempel om te wisselen: **{to_pct(trade_threshold)}**")
    lines.append(f"- Ingevoerde fees: **{fee_bps} bps** (≈ {to_pct(max(fee_bps/10_000.0,0.0))})\n")

    if advice == "SWITCH":
        lines.append(f"**Advies:** SWITCH → **{target}** (voordeel: {to_pct(diff)} t.o.v. huidige coin)")
    else:
        if target:
            lines.append(f"**Advies:** HOLD → al in **{target}** of voordeel < drempel")
        else:
            lines.append("**Advies:** HOLD (geen positie ingesteld of voordeel < drempel)")

    lines.append("\n> Let op: het **markttrendfilter** (RISK_ON/RISK_OFF) blijft separaat actief in de workflow.")
    (reports_dir / "top5_latest.md").write_text("\n".join(lines), encoding="utf-8")


def write_latest_summary(df_scores: pd.DataFrame, reports_dir: Path) -> None:
    best = df_scores.iloc[0:1].copy()
    best.to_csv(reports_dir / "latest.csv", index=False)
    with open(reports_dir / "latest.json", "w", encoding="utf-8") as f:
        json.dump(json.loads(best.to_json(orient="records")), f, ensure_ascii=False, indent=2)


# =========================================================
# Main
# =========================================================

def main():
    ap = argparse.ArgumentParser(description="Build crypto reports with cost-aware decision and RS-vs-BTC tiebreak")
    ap.add_argument("--limit", type=int, default=50, help="Aantal coins om te scoren (default 50)")
    ap.add_argument("--reports-dir", type=str, default="data/reports", help="Outputmap voor rapporten")
    ap.add_argument("--trade-threshold", type=float, default=0.05, help="Drempel om te wisselen (0.05=5%)")
    ap.add_argument("--current-coin", type=str, default="", help="Huidige positie (bv. LINK). Leeg = stable.")
    ap.add_argument("--fee-bps", type=int, default=52, help="Totale wisselkosten in bps (52 = 0.52%% roundtrip Kraken)")
    args = ap.parse_args()

    reports_dir = Path(args.reports_dir)
    ensure_dir(reports_dir)

    # 1) Scores verkrijgen
    df = compute_or_load_scores(limit=args.limit, reports_dir=reports_dir)

    # 2) Rangschikking met tie-breakers
    df_ranked = rank_with_tiebreakers(df)

    # 3) Bewaar volledige scores
    write_csv_json(df_ranked, reports_dir / "scores_latest.csv", reports_dir / "scores_latest.json")

    # 4) Beslissing o.b.v. threshold + fees (marktfilter blijft in workflow)
    advice = compute_decision(
        df_scores=df_ranked,
        current_coin=args.current_coin.strip() or None,
        trade_threshold=args.trade_threshold,
        fee_bps=args.fee_bps,
    )

    # 5) Top5 & latest
    write_top5_csv_md(df_ranked, reports_dir, advice, args.trade_threshold, args.fee_bps)
    write_latest_summary(df_ranked, reports_dir)

    # 6) Log
    best = df_ranked.iloc[0]
    print(f"[{now_iso()}] Best: {best['symbol']} (score={to_pct(float(best['score']))})")
    adv, tgt, best_score, diff = advice
    print(f"[{now_iso()}] Decision: {adv} → {tgt or '-'} (diff={to_pct(diff)}) "
          f"threshold>={to_pct(max(args.trade_threshold, args.fee_bps/10_000.0))}")


if __name__ == "__main__":
    main()

