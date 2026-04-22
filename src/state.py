# src/state.py
"""
Positie-state tracker met Take-Profit / Stop-Loss.

Slaat op in data/state/position.json:
{
    "symbol": "CHZ",
    "entry_price": 0.085,
    "entry_time": "2026-03-30T10:00:00Z",
    "entry_usd": 350.00,
    "peak_price": 0.102,
    "source": "dip_finder"
}

Exit-regels:
- Stop-loss:    -15% vanaf instapprijs (beschermt tegen doorzakken)
- Take-profit:  trailing stop 12% onder piek, activeert bij >= +20% winst
- Kraken noodrem: -20% stop-loss order op exchange (vangt nacht-crashes)
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional


STATE_DIR = Path(os.environ.get("STATE_DIR", "data/state"))
POSITION_FILE  = STATE_DIR / "position.json"
POSITIONS_FILE = STATE_DIR / "positions.json"  # multi-coin

# Cooldown
DEFAULT_COOLDOWN_HOURS = 48
OVERRIDE_ADVANTAGE_PCT = 10.0

# Take-Profit / Stop-Loss
STOP_LOSS_PCT = 0.15         # -15% vanaf entry
TRAILING_STOP_PCT = 0.25     # -25% vanaf piek (ruimer voor bull run)
TRAILING_ACTIVATE_PCT = 0.75  # trailing start pas bij +75% winst
KRAKEN_HARD_SL_PCT = 0.20    # -20% noodrem op Kraken exchange


# ---------------------------------------------------------------------------
# Positie laden / opslaan
# ---------------------------------------------------------------------------

def load_position() -> Optional[dict]:
    """Laad huidige positie uit state file."""
    if not POSITION_FILE.exists():
        return None
    try:
        data = json.loads(POSITION_FILE.read_text())
        if data.get("symbol"):
            return data
    except Exception:
        pass
    return None


def save_position(symbol: str, entry_price: float, entry_usd: float,
                  source: str = "pipeline") -> dict:
    """Sla nieuwe positie op."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    position = {
        "symbol": symbol.upper(),
        "entry_price": entry_price,
        "entry_usd": entry_usd,
        "entry_time": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "peak_price": entry_price,  # piek = instapprijs bij start
        "source": source,
    }
    POSITION_FILE.write_text(json.dumps(position, indent=2))
    return position


def clear_position() -> None:
    """Wis positie (na verkoop naar stablecoin)."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    POSITION_FILE.write_text(json.dumps({"symbol": None}, indent=2))
    POSITIONS_FILE.write_text(json.dumps([]))


# ---------------------------------------------------------------------------
# Multi-positie (diversificatie)
# ---------------------------------------------------------------------------

def load_positions() -> list:
    """Laad alle posities. Valt terug op enkele positie voor backwards compat."""
    if POSITIONS_FILE.exists():
        try:
            data = json.loads(POSITIONS_FILE.read_text())
            if isinstance(data, list):
                return [p for p in data if p.get("symbol")]
        except Exception:
            pass
    # Fallback: enkele positie
    pos = load_position()
    return [pos] if pos else []


def save_positions(positions: list) -> None:
    """Sla meerdere posities op."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    POSITIONS_FILE.write_text(json.dumps(positions, indent=2))
    # Sync: eerste positie ook naar enkelvoudig bestand (backwards compat)
    if positions:
        POSITION_FILE.write_text(json.dumps(positions[0], indent=2))
    else:
        POSITION_FILE.write_text(json.dumps({"symbol": None}, indent=2))


def clear_positions() -> None:
    """Wis alle posities."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    POSITIONS_FILE.write_text(json.dumps([]))
    POSITION_FILE.write_text(json.dumps({"symbol": None}, indent=2))


def update_peak_price(current_price: float) -> Optional[float]:
    """Update de piekprijs als de huidige prijs hoger is. Retourneert nieuwe piek."""
    pos = load_position()
    if not pos or not pos.get("symbol"):
        return None

    peak = pos.get("peak_price") or pos.get("entry_price", 0)
    if current_price > peak:
        pos["peak_price"] = current_price
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        POSITION_FILE.write_text(json.dumps(pos, indent=2))
        return current_price
    return peak


# ---------------------------------------------------------------------------
# Take-Profit / Stop-Loss checks
# ---------------------------------------------------------------------------

def check_stop_loss(current_price: float) -> dict:
    """
    Check of stop-loss is geraakt (-15% vanaf entry).

    Returns: {"triggered": bool, "reason": str, "loss_pct": float}
    """
    pos = load_position()
    if not pos or not pos.get("entry_price"):
        return {"triggered": False, "reason": "Geen positie"}

    entry = pos["entry_price"]
    change_pct = (current_price - entry) / entry

    if change_pct <= -STOP_LOSS_PCT:
        return {
            "triggered": True,
            "reason": (
                f"Stop-loss geraakt: {pos['symbol']} op "
                f"${current_price:.4f} ({change_pct*100:+.1f}% "
                f"vanaf entry ${entry:.4f})"
            ),
            "loss_pct": round(change_pct * 100, 1),
        }

    return {
        "triggered": False,
        "reason": f"P&L: {change_pct*100:+.1f}% (SL bij {-STOP_LOSS_PCT*100:.0f}%)",
        "loss_pct": round(change_pct * 100, 1),
    }


def check_take_profit(current_price: float) -> dict:
    """
    Check of take-profit trailing stop is geraakt.

    Logica:
    1. Update piekprijs
    2. Als winst >= +20%: activeer trailing stop
    3. Als prijs daalt 12% onder piek: verkoop

    Returns: {"triggered": bool, "reason": str, "profit_pct": float}
    """
    pos = load_position()
    if not pos or not pos.get("entry_price"):
        return {"triggered": False, "reason": "Geen positie"}

    entry = pos["entry_price"]
    peak = update_peak_price(current_price)

    total_change = (current_price - entry) / entry
    peak_change = (peak - entry) / entry
    drop_from_peak = (peak - current_price) / peak if peak > 0 else 0

    result = {
        "profit_pct": round(total_change * 100, 1),
        "peak_pct": round(peak_change * 100, 1),
        "drop_from_peak_pct": round(drop_from_peak * 100, 1),
    }

    # Trailing stop activeert pas bij >= +20% winst (piek)
    if peak_change >= TRAILING_ACTIVATE_PCT:
        if drop_from_peak >= TRAILING_STOP_PCT:
            result["triggered"] = True
            result["reason"] = (
                f"Take-profit! {pos['symbol']} piek was +{peak_change*100:.0f}%, "
                f"nu teruggevallen {drop_from_peak*100:.0f}% onder piek. "
                f"Winst veiliggesteld op {total_change*100:+.1f}%."
            )
        else:
            result["triggered"] = False
            result["reason"] = (
                f"Trailing stop actief: piek +{peak_change*100:.0f}%, "
                f"nu {drop_from_peak*100:.0f}% onder piek "
                f"(verkoopt bij {TRAILING_STOP_PCT*100:.0f}% drop)."
            )
    else:
        stop_price = round(peak * (1 - KRAKEN_HARD_SL_PCT), 4)
        result["triggered"] = False
        # Piek alleen tonen als die echt hoger was dan entry (> 0.5%)
        piek_str = f" | piek: +{peak_change*100:.0f}%" if peak_change > 0.005 and abs(peak_change - total_change) > 0.005 else ""
        result["reason"] = (
            f"P&L: {total_change*100:+.1f}%{piek_str} | Stop: ${stop_price:.4f}"
        )

    return result


def get_kraken_sl_price() -> Optional[float]:
    """Bereken de prijs voor de Kraken noodrem stop-loss order (-20%)."""
    pos = load_position()
    if not pos or not pos.get("entry_price"):
        return None
    return round(pos["entry_price"] * (1 - KRAKEN_HARD_SL_PCT), 6)


# ---------------------------------------------------------------------------
# Cooldown logica (ongewijzigd)
# ---------------------------------------------------------------------------

def hours_since_entry() -> Optional[float]:
    """Hoeveel uur geleden ben je ingestapt?"""
    pos = load_position()
    if not pos or not pos.get("entry_time"):
        return None
    try:
        entry = datetime.fromisoformat(pos["entry_time"].replace("Z", "+00:00"))
        now = datetime.now(entry.tzinfo)
        return (now - entry).total_seconds() / 3600.0
    except Exception:
        return None


def is_cooldown_active(cooldown_hours: float = DEFAULT_COOLDOWN_HOURS) -> bool:
    """Is de cooldown nog actief (te vroeg om te switchen)?"""
    hours = hours_since_entry()
    if hours is None:
        return False
    return hours < cooldown_hours


def should_switch(current_score: float, target_score: float,
                  cooldown_hours: float = DEFAULT_COOLDOWN_HOURS,
                  override_pct: float = OVERRIDE_ADVANTAGE_PCT) -> dict:
    """Bepaal of we moeten switchen, rekening houdend met cooldown."""
    hours = hours_since_entry()
    pos = load_position()
    advantage = 0.0

    if current_score > 0:
        advantage = ((target_score - current_score) / current_score) * 100.0

    result = {
        "advantage_pct": round(advantage, 1),
        "hours_in_position": round(hours, 1) if hours else None,
        "cooldown_active": False,
        "current_symbol": pos.get("symbol") if pos else None,
    }

    if not pos or not pos.get("symbol"):
        result["switch"] = True
        result["reason"] = "Geen huidige positie — vrij om in te stappen."
        return result

    if hours is not None and hours < cooldown_hours:
        remaining = cooldown_hours - hours
        result["cooldown_active"] = True
        if advantage >= override_pct:
            result["switch"] = True
            result["reason"] = (
                f"Cooldown actief ({hours:.0f}h van {cooldown_hours:.0f}h), "
                f"maar score-voordeel {advantage:.1f}% >= {override_pct}% — override."
            )
        else:
            voordeel_str = f"{advantage:.1f}%" if advantage > 0 else "onbekend (coin niet in top rankings)"
            result["switch"] = False
            result["reason"] = (
                f"Cooldown: {hours:.0f}h in positie, nog {remaining:.0f}h te gaan. "
                f"Score-voordeel: {voordeel_str} (override vereist {override_pct}%)."
            )
        return result

    # Als huidige coin niet in rankings staat (score=0) maar target wel → altijd switchen
    if current_score <= 0 and target_score > 0:
        result["switch"] = True
        result["reason"] = (
            f"Cooldown verlopen ({hours:.0f}h). "
            f"Huidige coin niet meer in rankings — switch naar sterkste coin."
        )
    elif advantage < 5.0:
        result["switch"] = False
        result["reason"] = (
            f"Voordeel ({advantage:.1f}%) is te klein om te switchen "
            f"(min 5% na fees)."
        )
    else:
        result["switch"] = True
        result["reason"] = (
            f"Cooldown verlopen ({hours:.0f}h). "
            f"Voordeel {advantage:.1f}% — switch aanbevolen."
        )

    return result
