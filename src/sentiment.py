# src/sentiment.py
"""
Sentiment-indicatoren: Fear & Greed Index en BTC Dominance.
Beide gratis en zonder API-key.
"""
from __future__ import annotations

import time
from typing import Any, Dict, Tuple

from src.utils import get


# ---------------------------------------------------------------------------
# Fear & Greed Index  (alternative.me — gratis, geen key)
# ---------------------------------------------------------------------------

def fear_greed_index() -> Tuple[float, float]:
    """
    Haal de huidige Fear & Greed Index op (0-100).
    Retourneert (score_0_to_1, age_hours).

    Mapping naar 0..1:
        0   = Extreme Fear   -> hoog koopsignaal   -> score ~0.9
        50  = Neutral                               -> score  0.5
        100 = Extreme Greed  -> hoog verkoopsignaal -> score ~0.1

    Contrair: angst = kansen, hebzucht = gevaar.
    """
    try:
        data = get("https://api.alternative.me/fng/?limit=1")
        entry = data["data"][0]
        value = int(entry["value"])  # 0..100
        ts = int(entry["timestamp"])
        age_hours = max(0.0, (time.time() - ts) / 3600.0)

        # Contraire mapping: lage FG (angst) = hoge score (koopsignaal)
        score = 1.0 - (value / 100.0)
        return (score, age_hours)
    except Exception:
        return (0.5, 0.0)  # neutraal bij fout


# ---------------------------------------------------------------------------
# BTC Dominance  (CoinGecko /global — gratis)
# ---------------------------------------------------------------------------

def btc_dominance_indicator() -> Tuple[float, float, float]:
    """
    Haal BTC dominance op. Lagere dominance = betere kansen voor alts.

    Retourneert (score_0_to_1, btc_dom_pct, age_hours).

    Mapping:
        BTC dom > 60%  -> alts onderpresteren -> score ~0.2
        BTC dom ~ 50%  -> neutraal            -> score ~0.5
        BTC dom < 40%  -> alt-season           -> score ~0.8

    Formule: lineaire mapping van [35%, 65%] naar [1.0, 0.0]
    """
    try:
        data = get("https://api.coingecko.com/api/v3/global")
        btc_dom = data["data"]["market_cap_percentage"]["btc"]

        # Lineair: 35% dom -> 1.0, 65% dom -> 0.0
        score = max(0.0, min(1.0, (65.0 - btc_dom) / 30.0))

        return (score, btc_dom, 0.0)
    except Exception:
        return (0.5, 50.0, 0.0)  # neutraal bij fout
