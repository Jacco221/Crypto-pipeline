# src/macro.py
"""
Macro-indicatoren — verbeterde versie.

Drie onafhankelijke signalen:
1. DXY (Dollar Index) trend — origineel, nu continu
2. Fear & Greed Index — nieuw
3. BTC Dominance — nieuw (kritisch voor alt-selectie)

Gecombineerde macro_score [0..1] als gewogen gemiddelde.
"""
from __future__ import annotations

import math
import time
from io import StringIO
from typing import Dict

import pandas as pd
import requests

from src.sentiment import fear_greed_index, btc_dominance_indicator


# ---------------------------------------------------------------------------
# DXY — nu continu i.p.v. ternair
# ---------------------------------------------------------------------------

def _fetch_stooq_csv() -> pd.DataFrame | None:
    urls = [
        "https://stooq.com/q/d/l/?s=usdidx&i=d",
        "https://stooq.com/q/d/l/?s=dxy&i=d",
    ]
    for url in urls:
        try:
            r = requests.get(url, timeout=20)
            if r.status_code == 200 and "Date,Open,High,Low,Close,Volume" in r.text:
                df = pd.read_csv(StringIO(r.text))
                df["Date"] = pd.to_datetime(df["Date"])
                df = df.sort_values("Date").reset_index(drop=True)
                return df
        except Exception:
            pass
    return None


def dxy_score_continuous() -> Dict[str, float]:
    """
    Continue DXY score [0..1].
    DXY dalend (SMA10 < SMA30) = bullish crypto = hoge score.
    Gebruikt tanh voor zachte overgang i.p.v. harde drempels.
    """
    df = _fetch_stooq_csv()
    if df is None or len(df) < 40:
        return {"dxy_score": 0.5, "dxy_raw": 0.0}

    df["SMA10"] = df["Close"].rolling(10).mean()
    df["SMA30"] = df["Close"].rolling(30).mean()
    last = df.dropna().iloc[-1]

    # Relatief verschil SMA10 vs SMA30
    diff = (float(last["SMA10"]) - float(last["SMA30"])) / float(last["SMA30"])

    # DXY omhoog = slecht voor crypto, dus inverteer
    # tanh(-20 * diff): als DXY stijgt (diff>0) -> negatief -> lage score
    score = (math.tanh(-20 * diff) + 1.0) / 2.0
    score = max(0.0, min(1.0, score))

    return {"dxy_score": round(score, 4), "dxy_raw": round(diff, 6)}


# ---------------------------------------------------------------------------
# Gecombineerde macro score
# ---------------------------------------------------------------------------

def macro_combined() -> Dict[str, float]:
    """
    Gecombineerde macro-score uit drie signalen:
        DXY trend:       30% gewicht
        Fear & Greed:    30% gewicht
        BTC Dominance:   40% gewicht (meest relevant voor alt-selectie)

    Retourneert dict met alle subscores en het eindresultaat.
    """
    # 1. DXY
    dxy = dxy_score_continuous()
    dxy_s = dxy["dxy_score"]

    # 2. Fear & Greed
    fg_s, fg_age = fear_greed_index()

    # 3. BTC Dominance
    dom_s, btc_dom_pct, dom_age = btc_dominance_indicator()

    # Gewogen combinatie
    macro_score = (
        0.30 * dxy_s +
        0.30 * fg_s +
        0.40 * dom_s
    )

    return {
        "dxy_score": dxy_s,
        "dxy_raw": dxy["dxy_raw"],
        "fg_score": round(fg_s, 4),
        "btc_dom_score": round(dom_s, 4),
        "btc_dom_pct": round(btc_dom_pct, 2),
        "macro_score": round(macro_score, 4),
    }


# Legacy interface
def dxy_indicator() -> tuple:
    """Backwards compatible: retourneert (score_int, ages, weights)."""
    dxy = dxy_score_continuous()
    s = dxy["dxy_score"]
    if s > 0.6:
        score_int = 1
    elif s < 0.4:
        score_int = -1
    else:
        score_int = 0
    return score_int, {"dxy": 0.0}, {"dxy": 1.0}
