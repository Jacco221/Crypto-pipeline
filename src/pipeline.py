# src/pipeline.py
"""
Pipeline orchestratie — scoort alle coins en combineert signalen.

Dit is het ontbrekende hart van de pipeline. Voorheen bestond dit
module niet, waardoor run.py altijd terugviel op scores_latest.csv.

Score-architectuur (verbeterd):
    TA       (35%): MA cross, volume trend, funding rate
    Momentum (25%): RS vs BTC 7d + 30d (continu)
    Macro    (25%): DXY + Fear&Greed + BTC Dominance
    (Liquiditeit kan later toegevoegd worden als Kraken API actief is)

Totaal: gewogen gemiddelde [0..1], gerapporteerd als percentage.
"""
from __future__ import annotations

import sys
import time
from typing import List, Optional

import numpy as np
import pandas as pd

from src.universe import get_top_coins
from src.ta import compute_ta, _clamp01
from src.rs import rs_combined
from src.macro import macro_combined
from src.utils import get


# ---------------------------------------------------------------------------
# OHLCV ophalen voor een coin (CoinGecko)
# ---------------------------------------------------------------------------

def _fetch_ohlcv(coin_id: str) -> Optional[pd.DataFrame]:
    """
    Haal OHLCV-data op via CoinGecko market_chart endpoint (prijzen + volumes).
    Gebruikt 365 dagen voor voldoende data voor MA200.
    """
    try:
        url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart"
        params = {"vs_currency": "usd", "days": "365", "interval": "daily"}
        data = get(url, params=params)

        prices = data.get("prices", [])
        volumes = data.get("total_volumes", [])
        if not prices:
            return None

        # Bouw DataFrame van prijzen
        pdf = pd.DataFrame(prices, columns=["ts", "close"])
        pdf["ts"] = pd.to_datetime(pdf["ts"], unit="ms").dt.normalize()
        pdf = pdf.set_index("ts").groupby(level=0).last()

        # Voeg volume toe
        if volumes:
            vdf = pd.DataFrame(volumes, columns=["ts", "volume"])
            vdf["ts"] = pd.to_datetime(vdf["ts"], unit="ms").dt.normalize()
            vdf = vdf.set_index("ts").groupby(level=0).last()
            pdf = pdf.join(vdf, how="left")
            pdf["volume"] = pdf["volume"].fillna(0)
        else:
            pdf["volume"] = 0

        # open/high/low zijn niet beschikbaar via market_chart,
        # maar ta.py gebruikt alleen close en volume
        pdf["open"] = pdf["close"]
        pdf["high"] = pdf["close"]
        pdf["low"] = pdf["close"]

        return pdf.sort_index()
    except Exception as e:
        print(f"  [WARN] OHLCV fetch mislukt voor {coin_id}: {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Score een enkele coin
# ---------------------------------------------------------------------------

def _score_coin(coin: dict, macro_data: dict) -> Optional[dict]:
    """
    Bereken de totaalscore voor een enkele coin.

    Score-gewichten:
        TA:       35%
        Momentum: 25%  (RS vs BTC)
        Macro:    25%  (DXY + FG + BTC Dom — gedeeld over alle coins)
    """
    coin_id = coin["id"]
    symbol = coin["symbol"]

    try:
        # 1. TA score
        ohlcv = _fetch_ohlcv(coin_id)
        if ohlcv is not None and len(ohlcv) >= 10:
            ta = compute_ta(ohlcv)
            ta_score = ta["ta_score"]
            ta_ma = ta["ma_crossover"]
            ta_vol = ta["volume_trend"]
            ta_fund = ta["funding_rate"]
        else:
            ta_score = 0.0
            ta_ma = 0.0
            ta_vol = 0.5
            ta_fund = 0.5

        # 2. RS score (7d + 30d gecombineerd)
        rs = rs_combined(coin_id)
        rs_score = rs["rs_score"]

        # 3. Macro score (al berekend, zelfde voor alle coins)
        macro_score = macro_data["macro_score"]

        # 4. Totaalscore
        total = (
            0.35 * ta_score +
            0.25 * rs_score +
            0.25 * macro_score
        )
        # Resterende 15% is gereserveerd voor liquiditeits-score (Kraken)
        # Voorlopig verdelen we die over de andere componenten
        total = total / 0.85  # normaliseer naar [0..1]
        total = _clamp01(total)

        return {
            "symbol": symbol,
            "name": coin["name"],
            "ta_ma": round(ta_ma, 4),
            "ta_volume": round(ta_vol, 4),
            "ta_funding": round(ta_fund, 4),
            "TA_%": round(ta_score * 100, 1),
            "RS_7d_%": round(rs["rs_7d"] * 100, 1),
            "RS_30d_%": round(rs["rs_30d"] * 100, 1),
            "Momentum_%": round(rs_score * 100, 1),
            "DXY_%": round(macro_data["dxy_score"] * 100, 1),
            "FG_%": round(macro_data["fg_score"] * 100, 1),
            "BTC_Dom_%": round(macro_data["btc_dom_score"] * 100, 1),
            "Macro_%": round(macro_score * 100, 1),
            "Total_%": round(total * 100, 1),
            "score": round(total, 4),
        }
    except Exception as e:
        print(f"  [WARN] Score mislukt voor {symbol}: {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Pipeline entry point
# ---------------------------------------------------------------------------

def build_scores(limit: int = 50) -> pd.DataFrame:
    """
    Volledige scoring pipeline:
    1. Haal top coins op (universe)
    2. Bereken macro (1x, gedeeld)
    3. Score elke coin individueel
    4. Retourneer DataFrame gesorteerd op score

    Dit is de functie die run.py aanroept via compute_scores_via_internal_modules().
    """
    print(f"[Pipeline] Start scoring voor top {limit} coins...")
    start = time.time()

    # 1. Universe
    coins = get_top_coins(limit=limit)
    print(f"[Pipeline] {len(coins)} coins opgehaald uit CoinGecko universe")

    # 2. Macro (1x berekenen, geldt voor alle coins)
    print("[Pipeline] Macro-indicatoren berekenen (DXY + Fear&Greed + BTC Dominance)...")
    macro_data = macro_combined()
    print(f"  DXY:  {macro_data['dxy_score']:.2f}  |  "
          f"F&G:  {macro_data['fg_score']:.2f}  |  "
          f"BTC Dom: {macro_data['btc_dom_pct']:.1f}% (score: {macro_data['btc_dom_score']:.2f})  |  "
          f"Macro totaal: {macro_data['macro_score']:.2f}")

    # 3. Score elke coin
    results: List[dict] = []
    for i, coin in enumerate(coins):
        print(f"  [{i+1}/{len(coins)}] {coin['symbol']}...", end=" ", flush=True)
        row = _score_coin(coin, macro_data)
        if row:
            results.append(row)
            print(f"score={row['Total_%']:.1f}%")
        else:
            print("SKIP")

        # Rate limit bescherming (CoinGecko free: ~10-30 calls/min)
        # Per coin: ~3 API calls (OHLCV + RS 7d + RS 30d)
        time.sleep(4)

    if not results:
        raise RuntimeError("Geen enkele coin kon gescoord worden.")

    df = pd.DataFrame(results)
    df = df.sort_values("score", ascending=False).reset_index(drop=True)

    elapsed = time.time() - start
    print(f"[Pipeline] Klaar in {elapsed:.0f}s — {len(df)} coins gescoord")

    return df
