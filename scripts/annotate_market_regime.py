#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Annotate market regime (RISK_ON / RISK_OFF) op basis van BTC Close vs MA(window).
Default window = 20 (MA20). Regel: RISK_ON als Close > MA én MA stijgt; anders RISK_OFF.

Voorbeeld gebruik (Actions of lokaal):
  python3 scripts/annotate_market_regime.py --out-md data/reports/top5_latest.md --window 20 --days 120
"""

import argparse
import json
import math
from pathlib import Path
from datetime import datetime, timezone
import urllib.request
import pandas as pd

CG_BASE = "https://api.coingecko.com/api/v3"

def http_get(url: str, timeout: int = 30):
    for i in range(5):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                return json.load(r)
        except Exception:
            if i == 4:
                raise
    return None

def fetch_btc_daily_prices(days: int = 120) -> pd.DataFrame:
    """
    Haal dagelijkse BTC USD closes via CoinGecko.
    Returned DataFrame met kolom 'close' en DateTime index (UTC, D-freq).
    """
    url = f"{CG_BASE}/coins/bitcoin/market_chart?vs_currency=usd&days={days}&interval=daily"
    data = http_get(url)
    if not data or "prices" not in data:
        raise RuntimeError("BTC data ophalen via CoinGecko mislukte")

    # data['prices'] = [[ts_ms, price], ...]
    rows = data["prices"]
    dt = [datetime.fromtimestamp(r[0] / 1000, tz=timezone.utc) for r in rows]
    close = [float(r[1]) for r in rows]
    df = pd.DataFrame({"close": close}, index=pd.to_datetime(dt))
    df = df.asfreq("D")  # force dag-freq (en vult niets bij)
    return df

def slope_up(series: pd.Series, lookback: int = 3) -> bool:
    """
    Eenvoudige slope: laatste MA > MA van 'lookback' dagen geleden.
    """
    if len(series.dropna()) < lookback + 1:
        return False
    last = series.iloc[-1]
    prev = series.iloc[-1 - lookback]
    return bool(last > prev)

def decide_regime_ma(close: pd.Series, ma: pd.Series, require_slope_up: bool = True) -> tuple[str, dict]:
    """
    Regime: RISK_ON als Close > MA én MA stijgt (indien require_slope_up); anders RISK_OFF.
    Returns (regime, info_dict)
    """
    c = float(close.iloc[-1])
    m = float(ma.iloc[-1]) if not math.isnan(ma.iloc[-1]) else float("nan")
    up = slope_up(ma) if require_slope_up else True
    regime = "RISK_ON" if (not math.isnan(m) and c > m and up) else "RISK_OFF"

    # afstand in %
    dist_pct = ((c - m) / m * 100.0) if (m and not math.isnan(m) and m != 0) else float("nan")
    info = {"close": c, "ma": m, "slope_up": up, "dist_pct": dist_pct}
    return regime, info

def to_markdown(out_md: Path, window: int, regime: str, info: dict):
    """
    Append een nette sectie aan het MD-rapport.
    """
    lines = []
    lines.append("\n---\n")
    lines.append(f"## Market regime – MA{window}\n")
    lines.append(f"**Regime:** **{regime}**\n")
    c = info.get("close")
    m = info.get("ma")
    dist = info.get("dist_pct")
    slope = "stijgend" if info.get("slope_up") else "dalend/vlak"
    lines.append(f"BTC prijs vs MA{window}: **{c:,.2f}** vs **{m:,.2f}**  ({dist:.2f}%)  \n")
    lines.append(f"_Trend MA{window}: {slope}_\n")
    lines.append("\n> Regel: RISK_ON als Close > MA en MA stijgt; anders RISK_OFF.\n")

    with open(out_md, "a", encoding="utf-8") as f:
        f.write("".join(lines))

def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--out-md", nargs="?", default="data/reports/top5_latest.md",
                   help="Pad naar output MD (default data/reports/top5_latest.md)")
    p.add_argument("--window", type=int, default=20, help="MA window (default 20)")
    p.add_argument("--days", type=int, default=120, help="Aantal dagen BTC data op te halen (default 120)")
    args = p.parse_args(argv)

    out_md = Path(args.out_md)

    # haal voldoende dagen op (minimaal window + wat marge)
    days = max(args.days, args.window + 10)
    df = fetch_btc_daily_prices(days=days)
    close = df["close"]
    ma = close.rolling(args.window).mean()

    regime, info = decide_regime_ma(close, ma, require_slope_up=True)
    to_markdown(out_md, args.window, regime, info)
    print(f"Regime: {regime}; close={info.get('close'):.2f}  ma{args.window}={info.get('ma'):.2f}  dist={info.get('dist_pct'):.2f}%")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"Fout in annotate_market_regime (MA20): {e}")
        raise

