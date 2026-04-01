#!/usr/bin/env python3
"""
Backtest — simuleer het trading systeem over het afgelopen jaar.

Simuleert:
- Dagelijkse scoring (TA + RS + Macro)
- Market regime (MA7/MA200 + F&G + DXY)
- Trade beslissingen (cooldown, fees)
- Vergelijkt met buy-and-hold BTC en ETH

Gebruik: python3 scripts/backtest.py --days 365 --start-capital 1000
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import numpy as np
import pandas as pd

from src.ta import _clamp01, weighted_group_score
from src.utils import get


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CACHE_DIR = Path("data/backtest")
FEE_PCT = 0.0052  # 0.52% roundtrip (Kraken spot)
COOLDOWN_DAYS = 2
OVERRIDE_PCT = 10.0

# Top coins om te backtesten (by CoinGecko ID)
TOP_COINS = [
    ("bitcoin", "BTC"), ("ethereum", "ETH"), ("binancecoin", "BNB"),
    ("ripple", "XRP"), ("solana", "SOL"), ("dogecoin", "DOGE"),
    ("cardano", "ADA"), ("avalanche-2", "AVAX"), ("chainlink", "LINK"),
    ("polkadot", "DOT"), ("tron", "TRX"), ("litecoin", "LTC"),
    ("uniswap", "UNI"), ("near", "NEAR"), ("sui", "SUI"),
    ("aptos", "APT"), ("render-token", "RENDER"), ("injective-protocol", "INJ"),
    ("fetch-ai", "FET"), ("arbitrum", "ARB"),
]


# ---------------------------------------------------------------------------
# Fase 1: Data ophalen + caching
# ---------------------------------------------------------------------------

def fetch_coin_history(coin_id: str, days: int = 365) -> pd.DataFrame:
    """Haal OHLCV op van CoinGecko, cache lokaal."""
    cache_file = CACHE_DIR / f"{coin_id}_{days}d.csv"

    if cache_file.exists():
        df = pd.read_csv(cache_file, parse_dates=["date"])
        if len(df) >= days * 0.8:  # 80% compleet = goed genoeg
            return df

    url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart"
    params = {"vs_currency": "usd", "days": str(days), "interval": "daily"}

    try:
        data = get(url, params=params)
    except Exception as e:
        print(f"  [WARN] Fetch mislukt voor {coin_id}: {e}")
        if cache_file.exists():
            return pd.read_csv(cache_file, parse_dates=["date"])
        return pd.DataFrame()

    prices = data.get("prices", [])
    volumes = data.get("total_volumes", [])
    mcaps = data.get("market_caps", [])

    if not prices:
        return pd.DataFrame()

    df = pd.DataFrame(prices, columns=["ts", "close"])
    df["date"] = pd.to_datetime(df["ts"], unit="ms").dt.normalize()
    df = df.groupby("date").last().reset_index()

    if volumes:
        vdf = pd.DataFrame(volumes, columns=["ts", "volume"])
        vdf["date"] = pd.to_datetime(vdf["ts"], unit="ms").dt.normalize()
        vdf = vdf.groupby("date").last().reset_index()[["date", "volume"]]
        df = df.merge(vdf, on="date", how="left")

    if mcaps:
        mdf = pd.DataFrame(mcaps, columns=["ts", "market_cap"])
        mdf["date"] = pd.to_datetime(mdf["ts"], unit="ms").dt.normalize()
        mdf = mdf.groupby("date").last().reset_index()[["date", "market_cap"]]
        df = df.merge(mdf, on="date", how="left")

    df = df[["date", "close", "volume", "market_cap"]].dropna(subset=["close"])

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(cache_file, index=False)
    return df


def fetch_fear_greed(days: int = 365) -> pd.DataFrame:
    """Haal historische Fear & Greed Index op."""
    cache_file = CACHE_DIR / f"fear_greed_{days}d.csv"
    if cache_file.exists():
        return pd.read_csv(cache_file, parse_dates=["date"])

    data = get(f"https://api.alternative.me/fng/?limit={days}")
    entries = data.get("data", [])

    rows = []
    for e in entries:
        try:
            ts = int(e["timestamp"])
            date = pd.to_datetime(ts, unit="s").normalize()
        except (ValueError, TypeError):
            date = pd.to_datetime(e["timestamp"]).normalize()
        rows.append({
            "date": date,
            "fg_value": int(e["value"]),
        })

    df = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(cache_file, index=False)
    return df


def fetch_dxy(days: int = 400) -> pd.DataFrame:
    """Haal DXY data op van Stooq."""
    cache_file = CACHE_DIR / f"dxy_{days}d.csv"
    if cache_file.exists():
        return pd.read_csv(cache_file, parse_dates=["date"])

    from io import StringIO
    import requests

    for url in ["https://stooq.com/q/d/l/?s=usdidx&i=d", "https://stooq.com/q/d/l/?s=dxy&i=d"]:
        try:
            r = requests.get(url, timeout=20)
            if r.status_code == 200 and "Date" in r.text:
                df = pd.read_csv(StringIO(r.text))
                df["date"] = pd.to_datetime(df["Date"]).dt.normalize()
                df = df.rename(columns={"Close": "dxy_close"})
                df = df[["date", "dxy_close"]].dropna().sort_values("date")
                CACHE_DIR.mkdir(parents=True, exist_ok=True)
                df.to_csv(cache_file, index=False)
                return df
        except Exception:
            continue

    return pd.DataFrame()


def fetch_funding_rate_history(days: int = 400) -> pd.DataFrame:
    """Haal historische BTC funding rates op van Binance Futures (gratis, geen key)."""
    cache_file = CACHE_DIR / f"funding_rate_{days}d.csv"
    if cache_file.exists():
        df = pd.read_csv(cache_file, parse_dates=["date"])
        if len(df) >= days * 0.7:
            return df

    import requests
    from datetime import timezone

    end_ts = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ts = end_ts - days * 24 * 3600 * 1000

    rows = []
    limit = 1000
    current_start = start_ts

    for _ in range(5):  # max 5 pagina's = 5000 records
        try:
            r = requests.get(
                "https://fapi.binance.com/fapi/v1/fundingRate",
                params={"symbol": "BTCUSDT", "startTime": current_start,
                        "endTime": end_ts, "limit": limit},
                timeout=15,
            )
            data = r.json()
            if not data:
                break
            for item in data:
                ts = int(item["fundingTime"])
                rate = float(item["fundingRate"]) * 100  # naar %
                date = pd.to_datetime(ts, unit="ms", utc=True).tz_localize(None).normalize()
                rows.append({"date": date, "funding_rate_pct": rate})
            if len(data) < limit:
                break
            current_start = int(data[-1]["fundingTime"]) + 1
            time.sleep(0.5)
        except Exception as e:
            print(f"  [WARN] Funding rate fetch fout: {e}")
            break

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    # Aggregeer naar daggemiddelde
    df = df.groupby("date")["funding_rate_pct"].mean().reset_index()
    df.columns = ["date", "funding_rate_pct"]
    df = df.sort_values("date").reset_index(drop=True)

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(cache_file, index=False)
    return df


def fetch_all_data(days: int = 365) -> Dict:
    """Haal alle data op voor de backtest."""
    print(f"[Backtest] Data ophalen voor {days} dagen...")

    # Fear & Greed
    print("  Fear & Greed Index...")
    fg = fetch_fear_greed(days)
    print(f"  → {len(fg)} dagen")

    # DXY
    print("  DXY (Dollar Index)...")
    dxy = fetch_dxy()
    print(f"  → {len(dxy)} dagen")

    # Funding Rate (Binance, gratis)
    print("  BTC Funding Rate (Binance)...")
    fr = fetch_funding_rate_history(days + 35)
    print(f"  → {len(fr)} dagen")

    # Coins
    coins = {}
    for i, (coin_id, symbol) in enumerate(TOP_COINS):
        print(f"  [{i+1}/{len(TOP_COINS)}] {symbol}...", end=" ", flush=True)
        df = fetch_coin_history(coin_id, days)
        if not df.empty:
            coins[symbol] = df
            print(f"{len(df)} dagen")
        else:
            print("SKIP")
        time.sleep(2)  # rate limit

    return {"coins": coins, "fg": fg, "dxy": dxy, "fr": fr}


# ---------------------------------------------------------------------------
# Fase 2: Scoring functies (dagelijkse data)
# ---------------------------------------------------------------------------

def _sigmoid(diff: float, sensitivity: float = 10.0) -> float:
    return 1.0 / (1.0 + math.exp(-sensitivity * diff))


def score_coin_on_day(symbol: str, coin_df: pd.DataFrame, btc_df: pd.DataFrame,
                      day_idx: int, macro_score: float) -> Optional[float]:
    """Score een coin op een specifieke dag."""
    if day_idx < 30 or day_idx >= len(coin_df):
        return None

    # ---- TA: MA7 crossover ----
    closes = coin_df["close"].values
    if day_idx < 7:
        return None

    current = closes[day_idx]
    ma7 = np.mean(closes[max(0, day_idx-6):day_idx+1])

    if ma7 > 0:
        cross = (current - ma7) / ma7
        ta_ma = (math.tanh(5 * cross) + 1.0) / 2.0
    else:
        ta_ma = 0.5

    # ---- TA: Volume trend ----
    volumes = coin_df["volume"].values if "volume" in coin_df else None
    if volumes is not None and day_idx >= 20:
        vol20 = np.mean(volumes[day_idx-19:day_idx+1])
        vol60 = np.mean(volumes[max(0, day_idx-59):day_idx+1]) if day_idx >= 60 else vol20
        vol_ratio = (vol20 / vol60) if vol60 > 0 else 1.0
        ta_vol = _clamp01(vol_ratio / 2.0)
    else:
        ta_vol = 0.5

    ta_score = 0.50 * ta_ma + 0.50 * ta_vol

    # ---- RS vs BTC (7d + 30d) ----
    if day_idx >= 30 and day_idx < len(btc_df):
        btc_closes = btc_df["close"].values

        # 7d RS
        if day_idx >= 7:
            coin_7d = (closes[day_idx] / closes[day_idx-7]) - 1.0
            btc_7d = (btc_closes[day_idx] / btc_closes[day_idx-7]) - 1.0
            rs_7d = _sigmoid(coin_7d - btc_7d)
        else:
            rs_7d = 0.5

        # 30d RS
        coin_30d = (closes[day_idx] / closes[day_idx-30]) - 1.0
        btc_30d = (btc_closes[day_idx] / btc_closes[day_idx-30]) - 1.0
        rs_30d = _sigmoid(coin_30d - btc_30d)

        rs_score = 0.4 * rs_7d + 0.6 * rs_30d
    else:
        rs_score = 0.5

    # ---- Totaal ----
    total = (0.35 * ta_score + 0.25 * rs_score + 0.25 * macro_score)
    total = total / 0.85
    return _clamp01(total)


def compute_regime_on_day(btc_df: pd.DataFrame, day_idx: int,
                          fg_value: int, dxy_bullish: bool,
                          funding_rate_pct: float = 0.0) -> Tuple[str, int]:
    """Bereken market regime op een specifieke dag (MA20 + funding rate)."""
    closes = btc_df["close"].values
    if day_idx < 200:
        return "RISK_OFF", 0

    score = 0

    # 1. BTC > MA20
    ma20 = np.mean(closes[max(0, day_idx-19):day_idx+1])
    if closes[day_idx] > ma20:
        score += 1

    # 2. MA20 > MA200
    ma200 = np.mean(closes[max(0, day_idx-199):day_idx+1])
    if ma20 > ma200:
        score += 1

    # 3. F&G > 25
    if fg_value > 25:
        score += 1

    # 4. DXY dalend
    if dxy_bullish:
        score += 1

    # 5. Funding rate (Stap 2)
    if funding_rate_pct < -0.05:   # extreem short = contrair koopsignaal
        score += 1
    elif funding_rate_pct > 0.10:  # extreem long = overbought
        score -= 1

    if score >= 3:
        return "RISK_ON", score
    elif score == 2:
        return "CAUTIOUS", score
    else:
        return "RISK_OFF", score


def compute_dxy_bullish(dxy_df: pd.DataFrame, date: pd.Timestamp) -> bool:
    """Check of DXY dalend is op een specifieke datum."""
    if dxy_df.empty:
        return False
    mask = dxy_df["date"] <= date
    recent = dxy_df[mask].tail(40)
    if len(recent) < 30:
        return False
    sma10 = recent["dxy_close"].tail(10).mean()
    sma30 = recent["dxy_close"].tail(30).mean()
    return sma10 < sma30  # DXY dalend = bullish crypto


def compute_macro_score(fg_value: int, dxy_bullish: bool, btc_dom: float = 50.0) -> float:
    """Bereken macro score uit historische componenten."""
    # F&G contrair
    fg_score = 1.0 - (fg_value / 100.0)
    # DXY
    dxy_score = 0.6 if dxy_bullish else 0.4
    # BTC Dom (gebruik geschatte constante, historisch niet beschikbaar per dag)
    dom_score = max(0.0, min(1.0, (65.0 - btc_dom) / 30.0))

    return 0.30 * dxy_score + 0.30 * fg_score + 0.40 * dom_score


# ---------------------------------------------------------------------------
# Fase 3: Simulatie
# ---------------------------------------------------------------------------

def run_backtest(data: Dict, start_capital: float = 1000.0,
                 fee_pct: float = FEE_PCT,
                 use_funding_rate: bool = True) -> Dict:
    """Draai de backtest simulatie."""
    coins_data = data["coins"]
    fg_df = data["fg"]
    dxy_df = data["dxy"]
    fr_df = data.get("fr", pd.DataFrame())

    btc_df = coins_data.get("BTC")
    if btc_df is None or btc_df.empty:
        raise RuntimeError("BTC data ontbreekt")

    # Aligneer alle data op BTC datums
    dates = btc_df["date"].values
    n_days = len(dates)

    print(f"\n[Backtest] Simulatie: {n_days} dagen, startkapitaal ${start_capital:.0f}")
    print(f"[Backtest] Fee per roundtrip: {fee_pct*100:.2f}%")

    # State
    cash = start_capital
    position = None  # {"symbol": "ETH", "amount": 5.2, "entry_idx": 100}
    last_switch_idx = -999

    # Regime cooldown: minimaal 3 dagen in een regime voordat we wisselen
    REGIME_COOLDOWN_DAYS = 3
    active_regime = "RISK_OFF"
    regime_since_idx = 0

    # Logging
    log = []
    trades = []
    regime_changes = []
    prev_regime = None

    # BTC buy-and-hold referentie
    btc_start_price = btc_df["close"].iloc[200]  # start na 200 dagen warmup
    eth_df = coins_data.get("ETH")
    eth_start_price = eth_df["close"].iloc[200] if eth_df is not None and len(eth_df) > 200 else None

    for day_idx in range(200, n_days):  # skip eerste 200 dagen (MA200 warmup)
        date = pd.Timestamp(dates[day_idx])

        # F&G voor deze dag
        fg_row = fg_df[fg_df["date"] <= date].tail(1)
        fg_value = int(fg_row["fg_value"].iloc[0]) if not fg_row.empty else 50

        # DXY
        dxy_bullish = compute_dxy_bullish(dxy_df, date)

        # Funding rate voor deze dag
        funding_rate_pct = 0.0
        if use_funding_rate and not fr_df.empty:
            fr_row = fr_df[fr_df["date"] <= date].tail(1)
            if not fr_row.empty:
                funding_rate_pct = float(fr_row["funding_rate_pct"].iloc[0])

        # Macro score
        macro = compute_macro_score(fg_value, dxy_bullish)

        # Regime (met cooldown: pas wisselen na 3 dagen stabiel signaal)
        raw_regime, regime_score = compute_regime_on_day(
            btc_df, day_idx, fg_value, dxy_bullish, funding_rate_pct
        )

        if raw_regime != active_regime:
            days_in_new = day_idx - regime_since_idx
            if raw_regime != prev_regime:
                # Nieuw signaal begint nu
                regime_since_idx = day_idx
            elif days_in_new >= REGIME_COOLDOWN_DAYS:
                # Signaal is stabiel genoeg → wissel regime
                regime_changes.append({
                    "date": str(date.date()),
                    "from": active_regime,
                    "to": raw_regime,
                    "score": regime_score,
                })
                active_regime = raw_regime
        else:
            regime_since_idx = day_idx

        prev_regime = raw_regime
        regime = active_regime

        # Score alle coins
        scores = {}
        for symbol, cdf in coins_data.items():
            if len(cdf) <= day_idx:
                continue
            s = score_coin_on_day(symbol, cdf, btc_df, day_idx, macro)
            if s is not None:
                scores[symbol] = s

        if not scores:
            continue

        best_symbol = max(scores, key=scores.get)
        best_score = scores[best_symbol]

        # Portfolio waarde berekenen
        if position:
            sym = position["symbol"]
            cdf = coins_data.get(sym)
            if cdf is not None and day_idx < len(cdf):
                current_price = cdf["close"].iloc[day_idx]
                portfolio_value = position["amount"] * current_price
            else:
                portfolio_value = cash
        else:
            portfolio_value = cash

        # ===== TRADE LOGICA =====
        action = "HOLD"
        days_since_switch = day_idx - last_switch_idx

        if regime == "RISK_OFF":
            # Verkoop alles
            if position:
                sym = position["symbol"]
                cdf = coins_data[sym]
                sell_price = cdf["close"].iloc[day_idx]
                proceeds = position["amount"] * sell_price
                fee = proceeds * (fee_pct / 2)  # halve roundtrip
                cash = proceeds - fee
                action = f"SELL {sym}"
                trades.append({
                    "date": str(date.date()), "action": "SELL", "symbol": sym,
                    "price": sell_price, "fee": fee, "cash_after": cash,
                })
                position = None
                last_switch_idx = day_idx

        elif regime in ("RISK_ON", "CAUTIOUS"):
            if not position and cash > 0:
                # Koop beste coin
                cdf = coins_data.get(best_symbol)
                if cdf is not None and day_idx < len(cdf):
                    buy_price = cdf["close"].iloc[day_idx]
                    fee = cash * (fee_pct / 2)
                    invest = cash - fee
                    amount = invest / buy_price
                    position = {"symbol": best_symbol, "amount": amount, "entry_idx": day_idx}
                    action = f"BUY {best_symbol}"
                    trades.append({
                        "date": str(date.date()), "action": "BUY", "symbol": best_symbol,
                        "price": buy_price, "fee": fee, "cash_after": 0,
                    })
                    cash = 0
                    last_switch_idx = day_idx

            elif position and position["symbol"] != best_symbol:
                # Check switch
                current_score = scores.get(position["symbol"], 0)
                advantage = ((best_score - current_score) / current_score * 100) if current_score > 0 else 0

                can_switch = False
                if days_since_switch >= COOLDOWN_DAYS:
                    can_switch = advantage >= 5.0
                elif advantage >= OVERRIDE_PCT:
                    can_switch = True

                if can_switch:
                    # Verkoop huidige
                    sym = position["symbol"]
                    sell_price = coins_data[sym]["close"].iloc[day_idx]
                    proceeds = position["amount"] * sell_price
                    sell_fee = proceeds * (fee_pct / 2)

                    # Koop nieuwe
                    buy_price = coins_data[best_symbol]["close"].iloc[day_idx]
                    buy_cash = proceeds - sell_fee
                    buy_fee = buy_cash * (fee_pct / 2)
                    amount = (buy_cash - buy_fee) / buy_price

                    total_fee = sell_fee + buy_fee
                    action = f"SWITCH {sym}→{best_symbol}"
                    trades.append({
                        "date": str(date.date()), "action": "SWITCH",
                        "from": sym, "to": best_symbol,
                        "sell_price": sell_price, "buy_price": buy_price,
                        "fee": total_fee, "cash_after": 0,
                    })
                    position = {"symbol": best_symbol, "amount": amount, "entry_idx": day_idx}
                    cash = 0
                    last_switch_idx = day_idx

        # Recalc portfolio value
        if position:
            sym = position["symbol"]
            cdf = coins_data.get(sym)
            if cdf is not None and day_idx < len(cdf):
                portfolio_value = position["amount"] * cdf["close"].iloc[day_idx]
        else:
            portfolio_value = cash

        log.append({
            "date": str(date.date()),
            "regime": regime,
            "regime_score": regime_score,
            "fg": fg_value,
            "best_coin": best_symbol,
            "best_score": round(best_score, 4),
            "position": position["symbol"] if position else "USD",
            "portfolio_value": round(portfolio_value, 2),
            "action": action,
        })

    # ===== Eindresultaten =====
    final_value = portfolio_value if position else cash
    btc_end_price = btc_df["close"].iloc[-1]
    btc_return = (btc_end_price / btc_start_price - 1) * 100

    eth_return = None
    if eth_df is not None and eth_start_price:
        eth_end_price = eth_df["close"].iloc[-1]
        eth_return = (eth_end_price / eth_start_price - 1) * 100

    strategy_return = (final_value / start_capital - 1) * 100

    # Max drawdown
    peak = start_capital
    max_dd = 0
    for entry in log:
        v = entry["portfolio_value"]
        if v > peak:
            peak = v
        dd = (peak - v) / peak * 100
        if dd > max_dd:
            max_dd = dd

    total_fees = sum(t.get("fee", 0) for t in trades)

    # Win rate
    wins = 0
    for i, t in enumerate(trades):
        if t["action"] == "SELL" and i > 0:
            # Vergelijk met vorige BUY
            for j in range(i-1, -1, -1):
                if trades[j]["action"] in ("BUY", "SWITCH"):
                    buy_val = trades[j].get("price", 0) or trades[j].get("buy_price", 0)
                    sell_val = t.get("price", 0)
                    if sell_val > buy_val:
                        wins += 1
                    break
    sell_count = sum(1 for t in trades if t["action"] == "SELL")
    win_rate = (wins / sell_count * 100) if sell_count > 0 else 0

    return {
        "start_capital": start_capital,
        "final_value": round(final_value, 2),
        "strategy_return_pct": round(strategy_return, 1),
        "btc_buyhold_return_pct": round(btc_return, 1),
        "eth_buyhold_return_pct": round(eth_return, 1) if eth_return else None,
        "max_drawdown_pct": round(max_dd, 1),
        "total_trades": len(trades),
        "total_fees": round(total_fees, 2),
        "win_rate_pct": round(win_rate, 1),
        "regime_changes": len(regime_changes),
        "days_simulated": len(log),
        "log": log,
        "trades": trades,
        "regime_changes_detail": regime_changes,
    }


# ---------------------------------------------------------------------------
# Fase 4: Rapport genereren
# ---------------------------------------------------------------------------

def write_results(results: Dict, output_dir: Path, results_old: Dict = None) -> None:
    """Schrijf backtest resultaten als Markdown rapport."""
    output_dir.mkdir(parents=True, exist_ok=True)

    # JSON voor analyse
    with open(output_dir / "backtest_results.json", "w") as f:
        json.dump(results, f, indent=2, default=str)

    # Markdown rapport
    lines = []
    lines.append("# Backtest Resultaten\n")
    lines.append(f"Periode: {results['days_simulated']} dagen\n")
    lines.append(f"Startkapitaal: ${results['start_capital']:.0f}\n")

    lines.append("\n## Rendement\n")
    lines.append(f"| Strategie | Rendement | Eindwaarde | Drawdown | Trades |")
    lines.append(f"|---|---|---|---|---|")
    lines.append(f"| **Nieuw systeem (+ funding rate)** | **{results['strategy_return_pct']:+.1f}%** | **${results['final_value']:.2f}** | {results['max_drawdown_pct']:.1f}% | {results['total_trades']} |")
    if results_old:
        lines.append(f"| Oud systeem (origineel) | {results_old['strategy_return_pct']:+.1f}% | ${results_old['final_value']:.2f} | {results_old['max_drawdown_pct']:.1f}% | {results_old['total_trades']} |")
    lines.append(f"| BTC buy-and-hold | {results['btc_buyhold_return_pct']:+.1f}% | ${results['start_capital'] * (1 + results['btc_buyhold_return_pct']/100):.2f} | — | — |")
    if results.get("eth_buyhold_return_pct") is not None:
        lines.append(f"| ETH buy-and-hold | {results['eth_buyhold_return_pct']:+.1f}% | ${results['start_capital'] * (1 + results['eth_buyhold_return_pct']/100):.2f} | — | — |")

    lines.append(f"\n## Risico\n")
    lines.append(f"- **Max drawdown:** {results['max_drawdown_pct']:.1f}%")
    lines.append(f"- **Totale fees:** ${results['total_fees']:.2f}")
    lines.append(f"- **Win rate:** {results['win_rate_pct']:.0f}%")

    lines.append(f"\n## Trades\n")
    lines.append(f"- Totaal: {results['total_trades']} trades")
    lines.append(f"- Regime-wisselingen: {results['regime_changes']}")

    if results["trades"]:
        lines.append(f"\n### Trade log\n")
        lines.append(f"| Datum | Actie | Details | Fee |")
        lines.append(f"|---|---|---|---|")
        for t in results["trades"]:
            if t["action"] == "BUY":
                lines.append(f"| {t['date']} | BUY | {t['symbol']} @ ${t['price']:.2f} | ${t['fee']:.2f} |")
            elif t["action"] == "SELL":
                lines.append(f"| {t['date']} | SELL | {t['symbol']} @ ${t['price']:.2f} | ${t['fee']:.2f} |")
            elif t["action"] == "SWITCH":
                lines.append(f"| {t['date']} | SWITCH | {t['from']}→{t['to']} | ${t['fee']:.2f} |")

    if results["regime_changes_detail"]:
        lines.append(f"\n### Regime-wisselingen\n")
        lines.append(f"| Datum | Van | Naar | Score |")
        lines.append(f"|---|---|---|---|")
        for rc in results["regime_changes_detail"]:
            lines.append(f"| {rc['date']} | {rc['from']} | {rc['to']} | {rc['score']}/4 |")

    (output_dir / "backtest_results.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\n[Backtest] Rapport geschreven naar {output_dir / 'backtest_results.md'}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Backtest crypto pipeline")
    ap.add_argument("--days", type=int, default=365, help="Aantal dagen")
    ap.add_argument("--start-capital", type=float, default=1000, help="Startkapitaal in USD")
    args = ap.parse_args()

    data = fetch_all_data(days=args.days)

    # Draai beide versies voor vergelijking
    print("\n[Backtest] Versie A: origineel (zonder funding rate)...")
    results_old = run_backtest(data, start_capital=args.start_capital, use_funding_rate=False)

    print("\n[Backtest] Versie B: nieuw (met funding rate)...")
    results_new = run_backtest(data, start_capital=args.start_capital, use_funding_rate=True)

    write_results(results_new, CACHE_DIR, results_old=results_old)

    # Samenvatting
    print(f"\n{'='*55}")
    print(f"BACKTEST VERGELIJKING")
    print(f"{'='*55}")
    print(f"{'Strategie':<30} {'Rendement':>10} {'Drawdown':>10} {'Trades':>7}")
    print(f"{'-'*55}")
    print(f"{'Nieuw (+ funding rate)':<30} {results_new['strategy_return_pct']:>+9.1f}% {results_new['max_drawdown_pct']:>9.1f}% {results_new['total_trades']:>7}")
    print(f"{'Oud (origineel)':<30} {results_old['strategy_return_pct']:>+9.1f}% {results_old['max_drawdown_pct']:>9.1f}% {results_old['total_trades']:>7}")
    print(f"{'BTC buy-and-hold':<30} {results_new['btc_buyhold_return_pct']:>+9.1f}%")
    if results_new.get("eth_buyhold_return_pct"):
        print(f"{'ETH buy-and-hold':<30} {results_new['eth_buyhold_return_pct']:>+9.1f}%")
    print(f"{'='*55}")
    delta = results_new['strategy_return_pct'] - results_old['strategy_return_pct']
    print(f"Verbetering nieuw vs oud: {delta:+.1f}%")


if __name__ == "__main__":
    main()
