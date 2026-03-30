# src/ta.py
from __future__ import annotations

from typing import Dict, Optional, TypedDict
import numpy as np
import pandas as pd


# ---------- Types ----------

class TAResult(TypedDict):
    ma_crossover: float   # [0..1]
    volume_trend: float   # [0..1]
    funding_rate: float   # [0..1]
    ta_score: float       # [0..1]


# ---------- Helpers ----------

def _clamp01(x: float) -> float:
    try:
        v = float(x)
    except Exception:
        return 0.0
    if np.isnan(v) or np.isinf(v):
        return 0.0
    return 0.0 if v < 0.0 else (1.0 if v > 1.0 else v)


def weighted_group_score(
    scores: Dict[str, float],
    weights: Dict[str, float],
    age_weights: Optional[Dict[str, float]] = None
) -> float:
    """
    Gemiddelde score met gewichten.

    - scores: dict met indicator → score (bv. [0..1])
    - weights: dict met indicator → gewicht (bv. 0.2, 0.4, …)
    - age_weights: optioneel dict met indicator → tijdsgewicht (bv. 0.5…3.0)

    Formule:
        som(score[k] * weight[k] * age_weight[k]) /
        som(weight[k] * age_weight[k])
    Ontbrekende scores worden overgeslagen.
    """
    num = 0.0
    den = 0.0
    for k, s in scores.items():
        w = float(weights.get(k, 0.0))
        if w == 0.0:
            continue
        t = 1.0
        if age_weights is not None:
            t = float(age_weights.get(k, 1.0))
        num += float(s) * w * t
        den += w * t
    return num / den if den > 0 else 0.0


# ---------- Hoofdindicator ----------

def compute_ta(ohlcv: pd.DataFrame) -> TAResult:
    """
    Verwacht kolommen: ['open','high','low','close','volume'] (en evt 'funding_rate').
    Levert drie subscores (ma_crossover, volume_trend, funding_rate) en een
    samengestelde ta_score — allemaal in [0..1].
    """
    if ohlcv is None or ohlcv.empty:
        return {"ma_crossover": 0.0, "volume_trend": 0.0, "funding_rate": 0.5, "ta_score": 0.0}

    df = ohlcv.copy()

    # Zorg voor numeriek + geen NaN aan het eind
    for col in ("close", "volume"):
        if col not in df:
            df[col] = np.nan
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.sort_index().dropna(subset=["close", "volume"])

    # ---- MA crossover (zachte 0..1 via tanh) ----
    ma7 = df["close"].rolling(7, min_periods=7).mean()
    ma200 = df["close"].rolling(200, min_periods=200).mean()
    x = (ma7 - ma200) / ma200.replace(0, np.nan)
    if len(x.dropna()) == 0:
        ma_cross = 0.5
    else:
        val = float(x.iloc[-1]) if pd.notna(x.iloc[-1]) else 0.0
        # tanh voor zachte overgang; schaal [-1..1] naar [0..1]
        ma_cross = (np.tanh(5 * val) + 1.0) / 2.0
    ma_cross = _clamp01(ma_cross)

    # ---- Volume trend (20-d gem / 60-d gem) ----
    vol20 = df["volume"].rolling(20, min_periods=20).mean().iloc[-1] if len(df) >= 20 else np.nan
    vol60 = df["volume"].rolling(60, min_periods=60).mean().iloc[-1] if len(df) >= 60 else np.nan
    if pd.notna(vol20) and pd.notna(vol60) and vol60 > 0:
        ratio = float(vol20 / vol60)
        # eenvoudige knijp naar [0..1], cap op 2x
        volume_trend = _clamp01(ratio / 2.0)
    else:
        volume_trend = 0.5

    # ---- Funding rate → [0..1] (0% = 0.5 neutraal; hogere funding -> lagere score) ----
    if "funding_rate" in df and pd.notna(df["funding_rate"].iloc[-1]):
        fr = float(pd.to_numeric(df["funding_rate"].iloc[-1], errors="coerce"))
    else:
        fr = 0.0
    # Map rond 0 met band ±0.1% → 5x factor; clamp naar [0..1]
    funding_score = _clamp01(0.5 + max(-0.5, min(0.5, -5.0 * fr)))

    # ---- Samengestelde TA via gewichten ----
    subs = {
        "ma_crossover": ma_cross,
        "volume_trend": volume_trend,
        "funding_rate": funding_score,
    }
    w = {
        "ma_crossover": 0.5,
        "volume_trend": 0.3,
        "funding_rate": 0.2,
    }
    ta_score = _clamp01(weighted_group_score(subs, w))

    return {
        "ma_crossover": float(ma_cross),
        "volume_trend": float(volume_trend),
        "funding_rate": float(funding_score),
        "ta_score": float(ta_score),
    }

