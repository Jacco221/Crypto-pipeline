# src/sentiment.py
"""
Sentiment-indicatoren: Fear & Greed Index, BTC Dominance en MVRV Ratio.
Fear & Greed + BTC Dominance: gratis en zonder API-key.
MVRV: Glassnode gratis API (vereist GLASSNODE_API_KEY env var) met MA365-proxy als fallback.
"""
from __future__ import annotations

import time
from typing import Any, Dict, Tuple

import requests

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


# ---------------------------------------------------------------------------
# MVRV Ratio  (Glassnode gratis tier — vereist API key)
# ---------------------------------------------------------------------------

def get_mvrv_ratio(btc_prices_series=None) -> Dict[str, Any]:
    """
    Haal MVRV ratio op voor BTC.

    MVRV = Market Value / Realized Value
        < 1.0  = markt onder reële waarde = historische koopzone   (bonus +1 punt)
        1.0–3.5 = neutraal                                          (geen aanpassing)
        > 3.5  = significant overgewaardeerd = bubble-zone          (hard RISK_OFF)

    Bronvolgorde:
        1. Glassnode gratis API (nauwkeurig, vereist GLASSNODE_API_KEY env var)
        2. MA365-proxy (prijs / 365-dag gemiddelde als benadering van realized price)
        3. Neutraal fallback (1.5) bij volledige fout

    Retourneert dict met: mvrv, source, buy_zone, bubble_zone, bonus_point
    """
    mvrv = None
    source = "unknown"

    # Echte MVRV vereist betaalde API (Glassnode/CoinMetrics).
    # We gebruiken direct de MA-proxy als beste gratis benadering.

    # --- Poging 2: MA365-proxy (geen API key nodig) ---
    if mvrv is None and btc_prices_series is not None:
        try:
            import pandas as pd
            s = btc_prices_series
            if len(s) >= 200:
                window = min(365, len(s))
                realized_proxy = float(s.rolling(window, min_periods=100).mean().iloc[-1])
                current_price = float(s.iloc[-1])
                if realized_proxy > 0:
                    mvrv = current_price / realized_proxy
                    source = f"ma{window}_proxy"
        except Exception:
            pass

    # --- Fallback: neutraal ---
    if mvrv is None:
        mvrv = 1.5
        source = "fallback_neutral"

    buy_zone = mvrv < 1.0
    bubble_zone = mvrv > 3.5
    bonus_point = 1 if buy_zone else 0

    return {
        "mvrv": round(mvrv, 3),
        "source": source,
        "buy_zone": buy_zone,       # MVRV < 1.0 → +1 regime punt
        "bubble_zone": bubble_zone, # MVRV > 3.5 → override naar RISK_OFF
        "bonus_point": bonus_point,
        "interpretation": (
            "koopzone (MVRV<1.0)" if buy_zone
            else "bubble-waarschuwing (MVRV>3.5)" if bubble_zone
            else "neutraal"
        ),
    }
