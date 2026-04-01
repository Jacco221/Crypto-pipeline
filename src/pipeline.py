# src/pipeline.py
"""
Pipeline orchestratie — scoort alle coins via bulk API calls.

Ontwerp: minimaal aantal API calls voor maximale snelheid.
- CoinGecko /coins/markets: 1 call per 250 coins (bevat prijs, volume, 7d/30d changes, sparkline)
- Macro (DXY + F&G + BTC Dom): 3 calls totaal
- Geen per-coin API calls nodig!

Score-architectuur:
    TA       (35%): MA7 cross (sparkline), volume trend
    Momentum (25%): RS vs BTC 7d + 30d (uit bulk price changes)
    Macro    (25%): DXY + Fear&Greed + BTC Dominance
"""
from __future__ import annotations

import math
import sys
import time
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from src.ta import _clamp01, weighted_group_score
from src.macro import macro_combined
from src.utils import get


# ---------------------------------------------------------------------------
# Bulk data ophalen (1 API call per 250 coins)
# ---------------------------------------------------------------------------

STABLES = {"usdt", "usdc", "busd", "dai", "tusd", "usde", "usdp", "fdusd", "pyusd"}


def _fetch_markets_bulk(limit: int = 150) -> List[dict]:
    """
    Haal alle coin-data op in bulk via /coins/markets.
    Inclusief sparkline (7d), price changes (1h/24h/7d/30d), volume.
    Max 250 per pagina.
    """
    all_coins: List[dict] = []
    per_page = min(limit + 20, 250)  # extra marge voor stablecoins die we uitfilteren
    pages = max(1, (limit + per_page - 1) // per_page)

    for page in range(1, pages + 1):
        url = "https://api.coingecko.com/api/v3/coins/markets"
        params = {
            "vs_currency": "usd",
            "order": "market_cap_desc",
            "per_page": per_page,
            "page": page,
            "sparkline": "true",
            "price_change_percentage": "1h,24h,7d,14d,30d",
        }
        data = get(url, params=params)
        if not isinstance(data, list):
            break

        for coin in data:
            sym = (coin.get("symbol") or "").lower()
            if sym in STABLES:
                continue
            all_coins.append(coin)
            if len(all_coins) >= limit:
                break

        if len(all_coins) >= limit:
            break
        time.sleep(1)  # kleine pauze tussen pagina's

    return all_coins


# ---------------------------------------------------------------------------
# TA scoring vanuit bulk data (geen extra API calls)
# ---------------------------------------------------------------------------

def _ta_from_sparkline(coin: dict) -> Dict[str, float]:
    """
    Bereken TA-subscores uit sparkline (7d) en volume data.

    MA7 crossover: vergelijk huidige prijs vs 7d gemiddelde (uit sparkline)
    Volume trend:  24h volume vs market cap ratio (liquiditeitsmetriek)
    """
    sparkline = coin.get("sparkline_in_7d", {}).get("price", [])

    # MA7 crossover uit sparkline
    if len(sparkline) >= 24:  # minimaal 1 dag aan data
        prices = np.array(sparkline, dtype=float)
        current = prices[-1]
        ma7 = np.mean(prices)  # gemiddelde over 7 dagen = MA7

        # Vergelijk ook met MA van eerste helft (proxy voor langere trend)
        half = len(prices) // 2
        ma_first_half = np.mean(prices[:half])
        ma_second_half = np.mean(prices[half:])

        # MA crossover: hoe ver boven/onder het gemiddelde
        if ma7 > 0:
            cross = (current - ma7) / ma7
            ma_cross = (math.tanh(5 * cross) + 1.0) / 2.0
        else:
            ma_cross = 0.5

        # Trend: stijgt het gemiddelde?
        if ma_first_half > 0:
            trend = (ma_second_half - ma_first_half) / ma_first_half
            trend_score = (math.tanh(10 * trend) + 1.0) / 2.0
        else:
            trend_score = 0.5
    else:
        ma_cross = 0.5
        trend_score = 0.5

    # Volume ratio (hoge volume/mcap = meer actief verhandeld)
    volume = coin.get("total_volume") or 0
    mcap = coin.get("market_cap") or 1
    vol_ratio = volume / mcap if mcap > 0 else 0
    # Typisch 0.01-0.20; map naar [0..1] met cap op 0.3
    volume_score = _clamp01(vol_ratio / 0.15)

    # Samengestelde TA
    subs = {"ma_crossover": ma_cross, "trend": trend_score, "volume": volume_score}
    weights = {"ma_crossover": 0.40, "trend": 0.35, "volume": 0.25}
    ta_score = _clamp01(weighted_group_score(subs, weights))

    return {
        "ta_ma": round(ma_cross, 4),
        "ta_trend": round(trend_score, 4),
        "ta_volume": round(volume_score, 4),
        "ta_score": round(ta_score, 4),
    }


# ---------------------------------------------------------------------------
# RS scoring vanuit bulk data (geen extra API calls)
# ---------------------------------------------------------------------------

def _sigmoid(diff: float, sensitivity: float = 10.0) -> float:
    return 1.0 / (1.0 + math.exp(-sensitivity * diff))


def _rs_from_bulk(coin: dict, btc_7d: float, btc_30d: float,
                  rs_7d_weight: float = 0.4, rs_30d_weight: float = 0.6) -> Dict[str, float]:
    """
    RS vs BTC uit de price_change_percentage velden.
    Continue sigmoid mapping.

    Gewichten worden aangepast op basis van BTC rotatie-signaal:
        ALT_SEASON  → rs_7d_weight=0.6 (momentum telt meer)
        BTC_SEASON  → rs_7d_weight=0.2 (alleen bewezen outperformers)
        NEUTRAL     → rs_7d_weight=0.4 (standaard)
    """
    coin_7d = coin.get("price_change_percentage_7d_in_currency") or 0.0
    coin_30d = coin.get("price_change_percentage_30d_in_currency") or 0.0

    diff_7d = (coin_7d - btc_7d) / 100.0
    diff_30d = (coin_30d - btc_30d) / 100.0

    rs_7d = _sigmoid(diff_7d)
    rs_30d = _sigmoid(diff_30d)

    rs_score = rs_7d_weight * rs_7d + rs_30d_weight * rs_30d

    return {
        "rs_7d": round(rs_7d, 4),
        "rs_30d": round(rs_30d, 4),
        "rs_score": round(rs_score, 4),
        "rs_diff_7d": round(diff_7d, 4),
        "rs_diff_30d": round(diff_30d, 4),
    }


# ---------------------------------------------------------------------------
# Score een coin uit bulk data
# ---------------------------------------------------------------------------

def _score_coin_bulk(coin: dict, btc_7d: float, btc_30d: float,
                     macro_data: dict, rotation: dict = None) -> dict:
    """Score een coin volledig uit bulk-data, zonder extra API calls."""
    symbol = (coin.get("symbol") or "").upper()
    name = coin.get("name", symbol)

    # 1. TA
    ta = _ta_from_sparkline(coin)

    # 2. Momentum (RS vs BTC) — gewichten afhankelijk van rotatie
    rs_7d_w  = rotation["rs_7d_weight"]  if rotation else 0.4
    rs_30d_w = rotation["rs_30d_weight"] if rotation else 0.6
    rs = _rs_from_bulk(coin, btc_7d, btc_30d, rs_7d_w, rs_30d_w)

    # 3. Macro (gedeeld)
    macro_score = macro_data["macro_score"]

    # 4. Totaalscore
    total = (
        0.35 * ta["ta_score"] +
        0.25 * rs["rs_score"] +
        0.25 * macro_score
    )
    total = total / 0.85  # normaliseer (15% reserve)
    total = _clamp01(total)

    return {
        "symbol": symbol,
        "name": name,
        "ta_ma": ta["ta_ma"],
        "ta_trend": ta["ta_trend"],
        "ta_volume": ta["ta_volume"],
        "TA_%": round(ta["ta_score"] * 100, 1),
        "RS_7d_%": round(rs["rs_7d"] * 100, 1),
        "RS_30d_%": round(rs["rs_30d"] * 100, 1),
        "Momentum_%": round(rs["rs_score"] * 100, 1),
        "DXY_%": round(macro_data["dxy_score"] * 100, 1),
        "FG_%": round(macro_data["fg_score"] * 100, 1),
        "BTC_Dom_%": round(macro_data["btc_dom_score"] * 100, 1),
        "Macro_%": round(macro_score * 100, 1),
        "Total_%": round(total * 100, 1),
        "score": round(total, 4),
    }


# ---------------------------------------------------------------------------
# Pipeline entry point
# ---------------------------------------------------------------------------

def build_scores(limit: int = 150) -> pd.DataFrame:
    """
    Volledige scoring pipeline via bulk API calls.

    API calls totaal: ~4-5 (ongeacht aantal coins)
    - 1x /coins/markets (tot 250 coins)
    - 1x Stooq DXY
    - 1x alternative.me Fear & Greed
    - 1x CoinGecko /global (BTC Dominance)

    Geschatte runtime: 5-15 seconden.
    """
    print(f"[Pipeline] Start bulk scoring voor top {limit} coins...")
    start = time.time()

    # 1. Bulk market data (1 API call)
    print("[Pipeline] Bulk market data ophalen...")
    coins = _fetch_markets_bulk(limit=limit)
    print(f"[Pipeline] {len(coins)} coins opgehaald van CoinGecko")

    if not coins:
        raise RuntimeError("Geen coins opgehaald van CoinGecko.")

    # 1b. Filter op Kraken-beschikbaarheid
    try:
        from src.kraken import get_tradeable_symbols
        kraken_symbols = get_tradeable_symbols()
        before = len(coins)
        coins = [c for c in coins if (c.get("symbol") or "").upper() in kraken_symbols]
        print(f"[Pipeline] Kraken filter: {before} → {len(coins)} verhandelbare coins")
    except Exception as e:
        print(f"[Pipeline] Kraken filter overgeslagen: {e}")

    # 2. BTC price changes (nodig als benchmark voor RS)
    btc_coin = next((c for c in coins if (c.get("symbol") or "").lower() == "btc"), None)
    btc_7d = btc_coin.get("price_change_percentage_7d_in_currency", 0.0) if btc_coin else 0.0
    btc_30d = btc_coin.get("price_change_percentage_30d_in_currency", 0.0) if btc_coin else 0.0
    print(f"[Pipeline] BTC benchmark: 7d={btc_7d:+.1f}%, 30d={btc_30d:+.1f}%")

    # 3. Macro (3 API calls totaal)
    print("[Pipeline] Macro-indicatoren berekenen...")
    macro_data = macro_combined()
    print(f"  DXY:  {macro_data['dxy_score']:.2f}  |  "
          f"F&G:  {macro_data['fg_score']:.2f}  |  "
          f"BTC Dom: {macro_data['btc_dom_pct']:.1f}% (score: {macro_data['btc_dom_score']:.2f})  |  "
          f"Macro totaal: {macro_data['macro_score']:.2f}")

    # 4. BTC Dominance Rotatie (uit bulk data — 0 extra API calls)
    from src.sentiment import get_btc_rotation
    rotation = get_btc_rotation(coins)
    print(f"[Pipeline] Rotatie: {rotation['rotation']} "
          f"(BTC 7d: {rotation['btc_7d']:+.1f}% vs alts mediaan: {rotation['alt_median_7d']:+.1f}%, "
          f"diff: {rotation['diff_pp']:+.1f}pp)")

    # 5. Score alle coins (geen extra API calls)
    print("[Pipeline] Scoring...")
    results: List[dict] = []
    for coin in coins:
        row = _score_coin_bulk(coin, btc_7d, btc_30d, macro_data, rotation)
        results.append(row)

    df = pd.DataFrame(results)
    df = df.sort_values("score", ascending=False).reset_index(drop=True)

    elapsed = time.time() - start
    print(f"[Pipeline] Klaar in {elapsed:.1f}s — {len(df)} coins gescoord")

    # Sla rotatie op voor andere modules (notify, trade_advisor)
    df.attrs["rotation"] = rotation

    return df
