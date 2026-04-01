# src/trade_log.py
"""
Trade log — elke uitgevoerde trade wordt opgeslagen voor performance tracking.

Format: data/state/trade_log.json (lijst van trade-records)

Record structuur:
{
    "datetime": "2026-04-01T10:00:00Z",
    "action": "BUY" | "SELL" | "SWITCH" | "STOP_LOSS" | "TAKE_PROFIT",
    "symbol": "CHZ",
    "price": 0.085,
    "amount_usd": 350.0,
    "pnl_pct": null,        # alleen bij verkoop
    "pnl_usd": null,        # alleen bij verkoop
    "entry_price": null,    # bij verkoop: wat was de instapprijs
    "source": "pipeline",   # pipeline | dip_finder | stop_loss | take_profit
    "txids": ["xxx"]
}
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


STATE_DIR = Path(os.environ.get("STATE_DIR", "data/state"))
TRADE_LOG_FILE = STATE_DIR / "trade_log.json"


def _load_log() -> list:
    if not TRADE_LOG_FILE.exists():
        return []
    try:
        data = json.loads(TRADE_LOG_FILE.read_text())
        if isinstance(data, list):
            return data
    except Exception:
        pass
    return []


def _save_log(log: list) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    TRADE_LOG_FILE.write_text(json.dumps(log, indent=2))


def log_trade(
    action: str,
    symbol: str,
    price: float,
    amount_usd: float,
    pnl_pct: Optional[float] = None,
    pnl_usd: Optional[float] = None,
    entry_price: Optional[float] = None,
    source: str = "pipeline",
    txids: Optional[list] = None,
) -> dict:
    """Log een uitgevoerde trade."""
    record = {
        "datetime": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "action": action,
        "symbol": symbol.upper(),
        "price": round(price, 6),
        "amount_usd": round(amount_usd, 2),
        "pnl_pct": round(pnl_pct, 2) if pnl_pct is not None else None,
        "pnl_usd": round(pnl_usd, 2) if pnl_usd is not None else None,
        "entry_price": round(entry_price, 6) if entry_price is not None else None,
        "source": source,
        "txids": txids or [],
    }
    log = _load_log()
    log.append(record)
    _save_log(log)
    return record


def get_performance_summary() -> dict:
    """
    Bereken performance statistieken uit de trade log.

    Returns dict met:
        total_trades        — totaal aantal trades (koop + verkoop)
        closed_trades       — aantal afgesloten posities (enkel verkooptrades)
        win_rate_pct        — % winstgevende closes
        total_pnl_usd       — totale winst/verlies in USD
        best_trade_pct      — beste trade in %
        worst_trade_pct     — slechtste trade in %
        avg_pnl_pct         — gemiddelde P&L per trade
        stop_losses         — aantal SL-hits
        take_profits        — aantal TP-hits
        log                 — ruwe trade lijst
    """
    log = _load_log()

    if not log:
        return {
            "total_trades": 0,
            "closed_trades": 0,
            "win_rate_pct": 0.0,
            "total_pnl_usd": 0.0,
            "best_trade_pct": None,
            "worst_trade_pct": None,
            "avg_pnl_pct": 0.0,
            "stop_losses": 0,
            "take_profits": 0,
            "log": [],
        }

    sells = [r for r in log if r.get("action") in ("SELL", "STOP_LOSS", "TAKE_PROFIT")
             and r.get("pnl_pct") is not None]

    pnls = [r["pnl_pct"] for r in sells]
    pnl_usds = [r["pnl_usd"] for r in sells if r.get("pnl_usd") is not None]

    winners = [p for p in pnls if p > 0]
    win_rate = (len(winners) / len(pnls) * 100) if pnls else 0.0

    return {
        "total_trades": len(log),
        "closed_trades": len(sells),
        "win_rate_pct": round(win_rate, 1),
        "total_pnl_usd": round(sum(pnl_usds), 2) if pnl_usds else 0.0,
        "best_trade_pct": round(max(pnls), 1) if pnls else None,
        "worst_trade_pct": round(min(pnls), 1) if pnls else None,
        "avg_pnl_pct": round(sum(pnls) / len(pnls), 1) if pnls else 0.0,
        "stop_losses": sum(1 for r in sells if r.get("action") == "STOP_LOSS"),
        "take_profits": sum(1 for r in sells if r.get("action") == "TAKE_PROFIT"),
        "log": log,
    }
