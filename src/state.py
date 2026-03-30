# src/state.py
"""
Positie-state tracker — onthoudt in welke coin je zit.

Slaat op in data/state/position.json:
{
    "symbol": "CHZ",
    "entry_price": 0.085,
    "entry_time": "2026-03-30T10:00:00",
    "entry_usd": 350.00,
    "source": "dip_finder"  // of "pipeline"
}

Dit voorkomt:
- Onnodig switchen direct na instap (cooldown)
- Verlies door heen-en-weer handelen
- Advies om te kopen wat je al hebt
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


STATE_DIR = Path(os.environ.get("STATE_DIR", "data/state"))
POSITION_FILE = STATE_DIR / "position.json"

# Cooldown: minimaal X uur na instap voordat we switchen
DEFAULT_COOLDOWN_HOURS = 48  # 2 dagen
# Override: alleen switchen als het voordeel >= X% is
OVERRIDE_ADVANTAGE_PCT = 10.0


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
        "source": source,
    }
    POSITION_FILE.write_text(json.dumps(position, indent=2))
    return position


def clear_position() -> None:
    """Wis positie (na verkoop naar stablecoin)."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    POSITION_FILE.write_text(json.dumps({"symbol": None}, indent=2))


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
        return False  # geen positie = geen cooldown
    return hours < cooldown_hours


def should_switch(current_score: float, target_score: float,
                  cooldown_hours: float = DEFAULT_COOLDOWN_HOURS,
                  override_pct: float = OVERRIDE_ADVANTAGE_PCT) -> dict:
    """
    Bepaal of we moeten switchen, rekening houdend met cooldown.

    Returns:
        {
            "switch": True/False,
            "reason": "...",
            "advantage_pct": 5.2,
            "cooldown_active": True/False,
            "hours_in_position": 36.5,
        }
    """
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

    # Geen positie → geen cooldown
    if not pos or not pos.get("symbol"):
        result["switch"] = True
        result["reason"] = "Geen huidige positie — vrij om in te stappen."
        return result

    # Cooldown actief?
    if hours is not None and hours < cooldown_hours:
        remaining = cooldown_hours - hours
        result["cooldown_active"] = True

        # Check override
        if advantage >= override_pct:
            result["switch"] = True
            result["reason"] = (
                f"Cooldown actief ({hours:.0f}h van {cooldown_hours:.0f}h), "
                f"maar voordeel is {advantage:.1f}% (>= {override_pct}%) — override toegestaan."
            )
        else:
            result["switch"] = False
            result["reason"] = (
                f"Cooldown actief: {hours:.0f}h in positie, nog {remaining:.0f}h te gaan. "
                f"Voordeel ({advantage:.1f}%) is te laag voor override (min {override_pct}%)."
            )
        return result

    # Cooldown verlopen — normaal evalueren
    if advantage < 5.0:
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
