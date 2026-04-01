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

    # --- MA365-proxy (geen API key nodig) ---
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


# ---------------------------------------------------------------------------
# BTC Dominance Rotatie  (berekend uit bulk coin data — 0 extra API calls)
# ---------------------------------------------------------------------------

def get_btc_rotation(coins: list) -> dict:
    """
    Bepaal of BTC of altcoins in de lead zijn op basis van 7-daags rendement.

    Logica:
        BTC_7d vs mediaan van top altcoins 7d (min $100M mcap, excl. stables)
        diff > +3pp  → BTC_SEASON  : BTC wint terrein, kies enkel sterke outperformers
        diff < -3pp  → ALT_SEASON  : alts winnen, meer altcoin kansen
        -3 tot +3pp  → NEUTRAL     : geen duidelijke rotatie

    Impact op scoring:
        ALT_SEASON  : RS_7d krijgt meer gewicht (snelle momentum telt)
        BTC_SEASON  : RS_30d krijgt meer gewicht (alleen bewezen outperformers)
        NEUTRAL     : standaard gewichten

    Retourneert dict met: rotation, btc_7d, alt_median_7d, diff_pp, rs_7d_weight, rs_30d_weight
    """
    import numpy as np

    btc = next((c for c in coins if (c.get("symbol") or "").lower() == "btc"), None)
    btc_7d = btc.get("price_change_percentage_7d_in_currency", 0.0) if btc else 0.0

    stables = {"usdt", "usdc", "busd", "dai", "tusd", "usde", "usdp", "fdusd",
               "pyusd", "btc", "eth"}
    alt_7ds = [
        c.get("price_change_percentage_7d_in_currency", 0.0)
        for c in coins
        if (c.get("symbol") or "").lower() not in stables
        and (c.get("market_cap") or 0) > 100_000_000
    ]

    if not alt_7ds:
        return {
            "rotation": "NEUTRAL", "btc_7d": btc_7d,
            "alt_median_7d": 0.0, "diff_pp": 0.0,
            "rs_7d_weight": 0.4, "rs_30d_weight": 0.6,
        }

    alt_median = float(np.median(alt_7ds))
    diff = btc_7d - alt_median

    if diff > 3.0:
        rotation = "BTC_SEASON"
        rs_7d_w, rs_30d_w = 0.2, 0.8   # bewezen lange-termijn outperformers
    elif diff < -3.0:
        rotation = "ALT_SEASON"
        rs_7d_w, rs_30d_w = 0.6, 0.4   # korte-termijn momentum telt
    else:
        rotation = "NEUTRAL"
        rs_7d_w, rs_30d_w = 0.4, 0.6   # standaard

    return {
        "rotation": rotation,
        "btc_7d": round(btc_7d, 2),
        "alt_median_7d": round(alt_median, 2),
        "diff_pp": round(diff, 2),
        "rs_7d_weight": rs_7d_w,
        "rs_30d_weight": rs_30d_w,
    }


# ---------------------------------------------------------------------------
# Funding Rate  (Binance Futures — gratis, geen key)
# ---------------------------------------------------------------------------

def get_funding_rate(symbol: str = "BTCUSDT") -> Dict[str, Any]:
    """
    Haal de huidige perpetual futures funding rate op via Binance (gratis, geen key).

    Funding rate = vergoeding die longs aan shorts betalen (of andersom) elke 8 uur.
    Meet hoeveel leverage/sentiment er in de markt zit.

    Drempels:
        > +0.10% per 8u  = extreem veel longs = overbought → -1 penalty
        -0.05% tot +0.10% = normaal           = neutraal   → geen aanpassing
        < -0.05% per 8u  = extreem veel shorts = oversold  → +1 bonus

    Retourneert dict met: rate_pct, signal, bonus_point, penalty_point, interpretation
    """
    try:
        r = requests.get(
            "https://fapi.binance.com/fapi/v1/premiumIndex",
            params={"symbol": symbol},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        rate = float(data["lastFundingRate"])  # bijv. 0.0001 = 0.01% per 8u
        rate_pct = rate * 100  # naar percentage

        overheated = rate_pct > 0.10   # extreem veel longs
        oversold   = rate_pct < -0.05  # extreem veel shorts

        bonus_point   = 1 if oversold   else 0
        penalty_point = 1 if overheated else 0

        if overheated:
            interpretation = f"overbought (funding {rate_pct:+.4f}%) → voorzichtig"
        elif oversold:
            interpretation = f"oversold (funding {rate_pct:+.4f}%) → contrair koopsignaal"
        else:
            interpretation = f"neutraal (funding {rate_pct:+.4f}%)"

        return {
            "rate_pct": round(rate_pct, 4),
            "signal": "OVERHEATED" if overheated else "OVERSOLD" if oversold else "NEUTRAL",
            "bonus_point": bonus_point,
            "penalty_point": penalty_point,
            "interpretation": interpretation,
            "source": "binance_futures",
        }

    except Exception:
        return {
            "rate_pct": 0.0,
            "signal": "NEUTRAL",
            "bonus_point": 0,
            "penalty_point": 0,
            "interpretation": "neutraal (fallback)",
            "source": "fallback",
        }
