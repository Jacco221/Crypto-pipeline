# src/token_unlocks.py
"""
Token unlock checker — controleert of er grote unlock events naderen.

Databron: Dropstab API (https://public-api.dropstab.com)
Gratis via Builders Program: dropstab.com/research/product/how-to-get-free-crypto-data-with-drops-tab-builders-program

Stel DROPSTAB_API_KEY in als GitHub Secret en lokale env variable.

Logica:
- Unlock >= 3% supply binnen 7 dagen  → BLOCK  (niet kopen)
- Unlock >= 3% supply binnen 30 dagen → WARNING (kopen met waarschuwing)
- Geen unlock of coin niet gedekt     → SAFE / UNKNOWN (kopen)
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

API_KEY = os.environ.get("DROPSTAB_API_KEY", "")
BASE_URL = "https://public-api.dropstab.com/api/v1"

# Symbol → Dropstab slug mapping voor coins die we handelen
# Aanvullen als we nieuwe coins toevoegen
SLUG_MAP: dict[str, str] = {
    "CFG":       "centrifuge",
    "XPL":       "xenoplasm",
    "RAVE":      "ravedao",
    "BTC":       "bitcoin",
    "ETH":       "ethereum",
    "SOL":       "solana",
    "DOT":       "polkadot",
    "ADA":       "cardano",
    "AVAX":      "avalanche",
    "LINK":      "chainlink",
    "ARB":       "arbitrum",
    "OP":        "optimism",
    "APT":       "aptos",
    "SUI":       "sui",
}

# Drempelwaarden
BLOCK_PCT   = 3.0   # >= 3% supply unlock binnen BLOCK_DAYS → niet kopen
WARNING_PCT = 3.0   # >= 3% supply unlock binnen WARNING_DAYS → waarschuwen
BLOCK_DAYS  = 7
WARNING_DAYS = 30


def _get_slug(symbol: str) -> Optional[str]:
    """Haal Dropstab slug op voor een coin symbol."""
    sym = symbol.upper()
    if sym in SLUG_MAP:
        return SLUG_MAP[sym]
    # Fallback: probeer lowercase symbol als slug
    return symbol.lower()


def check_upcoming_unlocks(symbol: str, days_ahead: int = WARNING_DAYS) -> dict:
    """
    Controleer op aankomende token unlock events voor een coin.

    Returns dict met:
        risk:     'SAFE' | 'WARNING' | 'BLOCK' | 'UNKNOWN'
        reason:   leesbare uitleg
        events:   lijst van aankomende unlock events
        symbol:   het gevraagde symbool
    """
    base_result = {"symbol": symbol, "events": [], "risk": "UNKNOWN",
                   "reason": "Check overgeslagen"}

    if not API_KEY:
        base_result["reason"] = (
            "DROPSTAB_API_KEY niet ingesteld — unlock check overgeslagen. "
            "Zie: dropstab.com/research/product/how-to-get-free-crypto-data-with-drops-tab-builders-program"
        )
        return base_result

    slug = _get_slug(symbol)
    if not slug:
        base_result["reason"] = f"{symbol} niet in slug-mapping — check overgeslagen"
        return base_result

    try:
        resp = requests.get(
            f"{BASE_URL}/tokenUnlocks/{slug}",
            headers={"Authorization": f"Bearer {API_KEY}"},
            timeout=10,
        )
        if resp.status_code == 404:
            base_result["risk"] = "UNKNOWN"
            base_result["reason"] = f"{symbol} niet gedekt door Dropstab — geen unlock data"
            return base_result
        if resp.status_code != 200:
            base_result["reason"] = f"Dropstab API fout: HTTP {resp.status_code}"
            return base_result

        data = resp.json().get("data", [])
    except Exception as e:
        base_result["reason"] = f"Dropstab API niet bereikbaar: {e}"
        return base_result

    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=days_ahead)
    upcoming = []

    for entry in data:
        try:
            event_date = datetime.fromisoformat(entry["date"].replace("Z", "+00:00"))
        except Exception:
            continue
        if now <= event_date <= cutoff:
            pct = float(entry.get("percentage", 0))
            upcoming.append({
                "date": event_date.strftime("%Y-%m-%d"),
                "days_until": (event_date - now).days,
                "percentage": pct,
                "amount": entry.get("amount", 0),
            })

    if not upcoming:
        return {
            "symbol": symbol,
            "events": [],
            "risk": "SAFE",
            "reason": f"Geen unlock events voor {symbol} in de komende {days_ahead} dagen ✅",
        }

    # Sorteer op datum
    upcoming.sort(key=lambda x: x["days_until"])
    nearest = upcoming[0]
    days_until = nearest["days_until"]
    pct = nearest["percentage"]

    # Bepaal risico
    if days_until <= BLOCK_DAYS and pct >= BLOCK_PCT:
        risk = "BLOCK"
        reason = (
            f"⛔ Unlock over {days_until} dag(en): {pct:.1f}% van supply vrijkomt op {nearest['date']}. "
            f"Aankoop geblokkeerd."
        )
    elif days_until <= WARNING_DAYS and pct >= WARNING_PCT:
        risk = "WARNING"
        reason = (
            f"⚠️ Unlock over {days_until} dagen: {pct:.1f}% van supply vrijkomt op {nearest['date']}. "
            f"Verhoogd verkooprisico."
        )
    else:
        risk = "SAFE"
        reason = (
            f"Eerstvolgende unlock over {days_until} dagen ({pct:.1f}% supply) — "
            f"ruim op tijd, geen bezwaar ✅"
        )

    return {"symbol": symbol, "events": upcoming, "risk": risk, "reason": reason}


def unlock_check_text(result: dict) -> str:
    """Telegram-leesbare weergave van unlock check resultaat."""
    emoji = {"SAFE": "✅", "WARNING": "⚠️", "BLOCK": "⛔", "UNKNOWN": "❓"}.get(result["risk"], "❓")
    return f"{emoji} Unlock check {result['symbol']}: {result['reason']}"
