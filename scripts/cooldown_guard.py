#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--md", required=True, help="Pad naar top5_latest.md")
    p.add_argument("--state", default="data/state/last_switch.json", help="JSON bestand met last_switch")
    p.add_argument("--cooldown-days", type=float, default=2.0)
    p.add_argument("--big-advantage", type=float, default=5.0, help="Override drempel (%)")
    p.add_argument("--mark-as_switched", action="store_true", help="Sla 'nu' als wisselmoment op")
    return p.parse_args()


def load_state(fp: Path) -> dict:
    if fp.exists():
        try:
            return json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(fp: Path, state: dict):
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(json.dumps(state, indent=2), encoding="utf-8")


def parse_advantage(md_text: str) -> float | None:
    # zoekt “voordeel: 88.4%”
    m = re.search(r"voordeel:\s*([0-9]+(?:\.[0-9]+)?)\s*%", md_text, re.IGNORECASE)
    return float(m.group(1)) if m else None


def ensure_cooldown_note(md_text: str, note_line: str) -> str:
    lines = md_text.splitlines()
    try:
        h_idx = next(i for i, l in enumerate(lines) if l.strip().lower().startswith("### cooldown"))
    except StopIteration:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("### Cooldown")
        h_idx = len(lines) - 1
    # verwijder bestaande bullets direct onder de header
    i = h_idx + 1
    while i < len(lines) and (lines[i].startswith("- ") or lines[i].startswith("• ") or not lines[i].strip()):
        if not lines[i].strip():
            i += 1
            break
        del lines[i]
    lines.insert(i, f"- {note_line}")
    return "\n".join(lines) + ("\n" if not md_text.endswith("\n") else "")


def to_aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_iso_aware(s: str | None) -> datetime | None:
    """Robuuste parser: ondersteunt 'Z' (UTC) en retourneert None bij ongeldige input."""
    if not s:
        return None
    try:
        s2 = s.strip().replace("Z", "+00:00")
        return datetime.fromisoformat(s2)
    except Exception:
        return None


def main() -> int:
    args = parse_args()
    md_file = Path(args.md)
    state_file = Path(args.state)

    # markeer directe wissel (handmatige override)
    state = load_state(state_file)
    now = utcnow()

    if args.mark_as_switched:
        state["last_switch"] = now.isoformat()
        save_state(state_file, state)
        print("✅ Wissel geregistreerd (handmatig).")
        return 0

    # lees md
    md_text = md_file.read_text(encoding="utf-8")

    # lees vorige wissel (tolerant voor 'Z' en ongeldige waarden)
    last_switch = state.get("last_switch")
    last_dt = parse_iso_aware(last_switch)

    # standaard: wissel toegestaan
    allowed = True
    note = "Wissel toegestaan (geen cooldown actief of override)."

    if last_dt:
        next_allowed = last_dt + timedelta(days=float(args.cooldown_days))
        if to_aware(now) < next_allowed:
            # cooldown actief → check override
            adv = parse_advantage(md_text)
            days_left = (next_allowed - now).total_seconds() / 86400.0
            if adv is not None and adv >= float(args.big_advantage):
                allowed = True
                note = f"Override bij ≥ {args.big_advantage:.1f}% voordeel. Nog ~{days_left:.2f} dag(en) cooldown."
            else:
                allowed = False
                if adv is None:
                    note = f"Wissel geblokkeerd door cooldown van {args.cooldown_days:.1f} dagen (voordeel onbekend). Nog ~{days_left:.2f} dag(en)."
                else:
                    note = (f"Wissel geblokkeerd door cooldown van {args.cooldown_days:.1f} dagen "
                            f"(voordeel {adv:.1f}% < {args.big_advantage:.1f}%). Nog ~{days_left:.2f} dag(en).")

    # schrijf notitie in MD (maar breek nooit de pipeline)
    try:
        new_md = ensure_cooldown_note(md_text, note)
        if new_md != md_text:
            md_file.write_text(new_md, encoding="utf-8")
    except Exception as e:
        md_file.write_text(md_text + f"\n\n*⚠️ Cooldown guard gaf een fout: {e}*\n", encoding="utf-8")

    print("✅ " + note)
    return 0


if __name__ == "__main__":
    sys.exit(main())

