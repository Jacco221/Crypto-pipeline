# src/rs.py
"""
Relative Strength vs BTC — verbeterde versie.

Veranderingen t.o.v. origineel:
1. Continue score (sigmoid) i.p.v. ternaire (-1/0/+1)
2. Twee tijdsperiodes: 7d (korte termijn) en 30d (middellange termijn)
3. Gecombineerde RS-score als gewogen gemiddelde
"""
from __future__ import annotations

import math
import time
from typing import Dict, Optional, Tuple

import pandas as pd

from src.utils import get


def _cg_market_chart(coin_id: str, days: int = 30) -> Optional[pd.DataFrame]:
    url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart"
    params = {"vs_currency": "usd", "days": days, "interval": "daily"}
    data = get(url, params=params)
    prices = data.get("prices", [])
    if not prices:
        return None
    df = pd.DataFrame(prices, columns=["ts", "price"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms")
    return df


def _sigmoid_score(diff: float, sensitivity: float = 10.0) -> float:
    """
    Zet een performance-verschil om naar een continue score [0..1].

    diff = 0    -> 0.5  (gelijk aan BTC)
    diff = +10% -> ~0.73 (outperformt BTC)
    diff = -10% -> ~0.27 (underperformt BTC)
    diff = +30% -> ~0.95
    diff = -30% -> ~0.05
    """
    return 1.0 / (1.0 + math.exp(-sensitivity * diff))


def rs_vs_btc_continuous(coin_id: str, days: int = 30) -> Tuple[float, float, Optional[float]]:
    """
    Continue RS vs BTC over opgegeven periode.

    Retourneert: (score_0_to_1, age_hours, raw_diff)
    """
    try:
        df_c = _cg_market_chart(coin_id, days)
        df_b = _cg_market_chart("bitcoin", days)
        if df_c is None or df_b is None or len(df_c) < 2 or len(df_b) < 2:
            return (0.5, 0.0, None)

        pc = (df_c["price"].iloc[-1] / df_c["price"].iloc[0]) - 1.0
        pb = (df_b["price"].iloc[-1] / df_b["price"].iloc[0]) - 1.0
        diff = pc - pb

        score = _sigmoid_score(diff)

        last_ts = df_c["ts"].iloc[-1].to_pydatetime().timestamp()
        age_hours = max(0.0, (time.time() - last_ts) / 3600.0)

        return (score, age_hours, diff)
    except Exception:
        return (0.5, 0.0, None)


def rs_combined(coin_id: str) -> Dict[str, float]:
    """
    Gecombineerde RS-score over 7d en 30d.

    Retourneert dict met:
        rs_7d:    korte-termijn RS score [0..1]
        rs_30d:   middellange-termijn RS score [0..1]
        rs_score: gewogen combinatie (7d: 40%, 30d: 60%)
        rs_diff_7d:  ruwe outperformance 7d
        rs_diff_30d: ruwe outperformance 30d
    """
    score_7d, age_7d, diff_7d = rs_vs_btc_continuous(coin_id, days=7)
    score_30d, age_30d, diff_30d = rs_vs_btc_continuous(coin_id, days=30)

    # Gewogen combinatie: 30d weegt zwaarder (stabielere trend)
    rs_score = 0.4 * score_7d + 0.6 * score_30d

    return {
        "rs_7d": round(score_7d, 4),
        "rs_30d": round(score_30d, 4),
        "rs_score": round(rs_score, 4),
        "rs_diff_7d": round(diff_7d, 4) if diff_7d is not None else None,
        "rs_diff_30d": round(diff_30d, 4) if diff_30d is not None else None,
    }


# Backwards compatibility
def rs_vs_btc_indicator(coin_id: str) -> tuple:
    """Legacy interface — retourneert (score_int, ages, weights)."""
    result = rs_combined(coin_id)
    s = result["rs_score"]
    if s > 0.6:
        score_int = 1
    elif s < 0.4:
        score_int = -1
    else:
        score_int = 0
    return score_int, {"rs": 0.0}, {"rs": 1.0}
