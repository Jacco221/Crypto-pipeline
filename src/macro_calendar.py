# src/macro_calendar.py
"""
Macro kalender — voorspelbare marktrisico events.

Fed vergaderingen en CPI releases staan maanden van tevoren vast.
Crypto daalt structureel in de aanloop naar deze events.

Logica:
- Event binnen 2 dagen → regime tijdelijk naar CAUTIOUS (ook als score RISK_ON zegt)
- Event binnen 1 dag  → waarschuwing in Telegram bericht

Kalender bijwerken: voeg nieuwe datums toe aan FED_DATES en CPI_DATES.
Fed kalender: federalreserve.gov/monetarypolicy/fomccalendars.htm
CPI kalender: bls.gov/schedule/news_release/cpi.htm
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

# ── Federal Reserve FOMC vergaderingen (beslissing dag) ──────────────────────
FED_DATES = [
    # 2026
    date(2026, 1, 29),
    date(2026, 3, 19),
    date(2026, 4, 29),  # aankomend
    date(2026, 6, 11),
    date(2026, 7, 29),
    date(2026, 9, 17),
    date(2026, 11, 5),
    date(2026, 12, 16),
]

# ── US CPI releases ───────────────────────────────────────────────────────────
CPI_DATES = [
    # 2026
    date(2026, 1, 15),
    date(2026, 2, 12),
    date(2026, 3, 12),
    date(2026, 4, 15),  # aankomend
    date(2026, 5, 13),
    date(2026, 6, 11),
    date(2026, 7, 15),
    date(2026, 8, 12),
    date(2026, 9, 11),
    date(2026, 10, 14),
    date(2026, 11, 12),
    date(2026, 12, 11),
]

WARN_DAYS  = 2   # waarschuwing en CAUTIOUS override binnen N dagen
BLOCK_DAYS = 1   # extra waarschuwing dag-van


def check_macro_events(today: Optional[date] = None) -> dict:
    """
    Controleer of er macro events naderen.

    Returns:
        risk:    'SAFE' | 'CAUTION' | 'HIGH'
        events:  lijst van naderende events
        reason:  leesbare uitleg
        override_cautious: True als regime tijdelijk CAUTIOUS moet zijn
    """
    if today is None:
        today = date.today()

    upcoming = []
    for d in FED_DATES:
        days_until = (d - today).days
        if 0 <= days_until <= WARN_DAYS:
            upcoming.append({
                "type": "Fed vergadering",
                "date": str(d),
                "days_until": days_until,
            })

    for d in CPI_DATES:
        days_until = (d - today).days
        if 0 <= days_until <= WARN_DAYS:
            upcoming.append({
                "type": "CPI release",
                "date": str(d),
                "days_until": days_until,
            })

    upcoming.sort(key=lambda x: x["days_until"])

    if not upcoming:
        return {
            "risk": "SAFE",
            "events": [],
            "reason": "Geen macro events in de komende 2 dagen ✅",
            "override_cautious": False,
        }

    nearest = upcoming[0]
    days = nearest["days_until"]
    etype = nearest["type"]
    edate = nearest["date"]

    if days == 0:
        risk = "HIGH"
        reason = f"🚨 {etype} VANDAAG ({edate}) — verhoogde volatiliteit verwacht"
    elif days == 1:
        risk = "HIGH"
        reason = f"⚠️ {etype} morgen ({edate}) — markt kan nerveus reageren"
    else:
        risk = "CAUTION"
        reason = f"📅 {etype} over {days} dagen ({edate}) — regime tijdelijk CAUTIOUS"

    return {
        "risk": risk,
        "events": upcoming,
        "reason": reason,
        "override_cautious": True,  # forceer CAUTIOUS ook als score RISK_ON zegt
    }


def macro_note(today: Optional[date] = None) -> str:
    """Korte leesbare samenvatting voor Telegram berichten."""
    result = check_macro_events(today)
    if result["risk"] == "SAFE":
        return ""
    emoji = "🚨" if result["risk"] == "HIGH" else "📅"
    return f"{emoji} Macro: {result['reason']}"
