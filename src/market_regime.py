# src/market_regime.py
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime as dt
from typing import Any, Dict, List

import pandas as pd
import requests

# ===== Instellingen =====
S_NAME = "BTC_USD"
# CoinGecko endpoint – kan override worden via env var COINGECKO_API_URL
COINGECKO_URL = os.environ.get(
    "COINGECKO_API_URL",
    "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart",
)
VS_CURRENCY = "usd"

# ===== Helpers =====
def _sma(series: pd.Series, window: int) -> pd.Series:
    # Stevige rolling mean (min_periods > 1 om gapjes te voorkomen)
    return series.rolling(window, min_periods=max(5, window // 5)).mean()

def _fetch_btc_prices(days: int = 300) -> pd.Series:
    """
    Haal historische BTC closing prices (USD) op via CoinGecko.
    Robuust met retries, backoff en nette User-Agent.
    Retourneert een pandas Series met UTC-datums als index en float closes als waarden.
    """
    params = {"vs_currency": VS_CURRENCY, "days": str(days)}
    headers = {
        "Accept": "application/json",
        # Sommige API's blokkeren standaard UA's op CI-runners
        "User-Agent": "crypto-pipeline/1.0 (+https://github.com/Jacco221/Crypto-pipeline)",
    }
    retries = 6
    backoff = 2.0

    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(COINGECKO_URL, params=params, headers=headers, timeout=30)
            # 429 -> rate limit. 5xx -> server issues.
            if r.status_code in (429, 500, 502, 503, 504):
                # exponential backoff met extra jitter
                sleep_s = backoff * attempt + 0.5
                time.sleep(sleep_s)
                continue
            r.raise_for_status()
            data = r.json()

            # Verwacht schema: {"prices": [[ts_ms, price], ...]}
            prices: List[List[float]] = data.get("prices", [])
            if not prices:
                raise RuntimeError("CoinGecko response heeft geen 'prices' veld.")

            # Zet om naar Series met datumindex (UTC) en float waarden
            idx = [
                pd.to_datetime(int(ts_ms), unit="ms", utc=True).normalize()
                for ts_ms, _ in prices
            ]
            vals = [float(price) for _, price in prices]
            s = pd.Series(vals, index=idx, name=S_NAME)

            # Combineer dubbele dagen (soms meerdere entries per dag)
            s = s.groupby(s.index).last().sort_index()
            return s
        except Exception as e:
            last_exc = e
            # Exponential backoff
            sleep_s = backoff * attempt + 0.5
            time.sleep(sleep_s)

    # Als we hier komen is het niet gelukt
    raise RuntimeError(f"BTC price fetch is mislukt na retries. Laatste fout: {last_exc}")

def determine_market_regime(
    days: int = 300,
    short_ma: int = 7,
    long_ma: int = 200,
) -> Dict[str, Any]:
    """
    Bepaalt marktregime met drie niveaus:

    Score-systeem (0-4 punten):
        +1 als BTC > MA7
        +1 als MA7 > MA200
        +1 als Fear & Greed > 25 (niet extreme angst)
        +1 als DXY dalend (SMA10 < SMA30)

    Regime:
        score >= 3 -> RISK_ON   (volledige allocatie)
        score == 2 -> CAUTIOUS  (halve allocatie)
        score <= 1 -> RISK_OFF  (alles stablecoin)
    """
    try:
        s = _fetch_btc_prices(days=days)
    except Exception as e:
        return {
            "regime": "RISK_OFF",
            "regime_score": 0,
            "error": f"fetch_failed: {e}",
            "insufficient_history": True,
            "as_of": dt.utcnow().isoformat(timespec="seconds") + "Z",
            "source": "CoinGecko",
        }

    if len(s) < max(short_ma, long_ma) + 5:
        return {
            "regime": "RISK_OFF",
            "regime_score": 0,
            "error": "insufficient_history",
            "as_of": dt.utcnow().isoformat(timespec="seconds") + "Z",
            "source": "CoinGecko",
        }

    ma7 = _sma(s, short_ma)
    ma200 = _sma(s, long_ma)

    last_close = float(s.iloc[-1])
    last_ma7 = float(ma7.iloc[-1])
    last_ma200 = float(ma200.iloc[-1])

    # Score-systeem
    regime_score = 0
    signals = {}

    # 1. BTC boven MA7
    btc_above_ma7 = last_close > last_ma7
    if btc_above_ma7:
        regime_score += 1
    signals["btc_above_ma7"] = btc_above_ma7

    # 2. MA7 boven MA200
    ma7_above_ma200 = last_ma7 > last_ma200
    if ma7_above_ma200:
        regime_score += 1
    signals["ma7_above_ma200"] = ma7_above_ma200

    # 3. Fear & Greed niet in extreme angst
    try:
        from src.sentiment import fear_greed_index
        fg_score, _ = fear_greed_index()
        fg_value = int((1.0 - fg_score) * 100)  # terug naar 0-100 schaal
        fg_not_extreme_fear = fg_value > 25
        if fg_not_extreme_fear:
            regime_score += 1
        signals["fear_greed_value"] = fg_value
        signals["fg_not_extreme_fear"] = fg_not_extreme_fear
    except Exception:
        signals["fear_greed_value"] = None
        signals["fg_not_extreme_fear"] = None

    # 4. DXY dalend
    try:
        from src.macro import dxy_score_continuous
        dxy = dxy_score_continuous()
        dxy_bullish = dxy["dxy_score"] > 0.55  # DXY dalend = bullish crypto
        if dxy_bullish:
            regime_score += 1
        signals["dxy_score"] = dxy["dxy_score"]
        signals["dxy_bullish"] = dxy_bullish
    except Exception:
        signals["dxy_score"] = None
        signals["dxy_bullish"] = None

    # Regime bepalen
    if regime_score >= 3:
        regime = "RISK_ON"
    elif regime_score == 2:
        regime = "CAUTIOUS"
    else:
        regime = "RISK_OFF"

    return {
        "regime": regime,
        "regime_score": regime_score,
        "signals": signals,
        "as_of": dt.utcnow().isoformat(timespec="seconds") + "Z",
        "last_close": round(last_close, 2),
        "ma7": round(last_ma7, 2),
        "ma200": round(last_ma200, 2),
        "rule": "score>=3->RISK_ON, ==2->CAUTIOUS, <=1->RISK_OFF",
        "source": "CoinGecko + alternative.me + Stooq",
    }

# Kleine CLI om JSON te printen (handig in GH Actions / lokaal)
def main(argv: list[str]) -> int:
    out = determine_market_regime()
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0

if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

