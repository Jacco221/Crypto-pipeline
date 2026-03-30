#!/usr/bin/env python3
"""
Cooldown Guard: voorkomt te snel wisselen van munten
- Houdt bij wanneer de laatste wissel plaatsvond
- Controleert of voldoende dagen verstreken zijn (cooldown)
- Optioneel: sta wissel eerder toe bij groot voordeel
"""

import argparse
import json
from pathlib import Path
from datetime import datetime, timedelta


def load_state(state_file: Path):
    """Laad de state (laatste wissel)."""
    if state_file.exists():
        with open(state_file) as f:
            return json.load(f)
    return {"last_switch": None}


def save_state(state_file: Path, state):
    """Sla de state op (laatste wissel)."""
    with open(state_file, "w") as f:
        json.dump(state, f)


def parse_top5(md_file: Path):
    """Lees de laatste adviesregel uit het top5-rapport."""
    with open(md_file) as f:
        lines = f.readlines()

    # Zoek regel met advies
    advice_line = next((l.strip() for l in reversed(lines) if l.startswith("**Advies:**")), None)
    return advice_line


def main():
    parser = argparse.ArgumentParser(description="Cooldown guard voor coin switches")
    parser.add_argument("--md", type=Path, required=True, help="Markdown rapport (top5_latest.md)")
    parser.add_argument("--state", type=Path, required=True, help="State-bestand (json)")
    parser.add_argument("--cooldown-days", type=float, default=2.0, help="Aantal dagen cooldown")
    parser.add_argument("--big-advantage", type=float, default=5.0, help="Extra voordeel (in %) om cooldown te overslaan")
    parser.add_argument("--mark_as_switched", action="store_true", help="Markeer nu als wissel")

    args = parser.parse_args()

    state = load_state(args.state)
    last_switch = None
    if state.get("last_switch"):
        last_switch = datetime.fromisoformat(state["last_switch"])

    now = datetime.utcnow()
    advice = parse_top5(args.md)

    if args.mark_as_switched:
        # Wissel uitvoeren → state opslaan
        state["last_switch"] = now.isoformat()
        save_state(args.state, state)
        print(f"✅ Wissel geregistreerd op {now.isoformat()}")
        return

    # Bereken cooldown
    if last_switch:
        next_allowed = last_switch + timedelta(days=args.cooldown_days)
        if now < next_allowed:
            print(f"⏳ Cooldown actief tot {next_allowed.isoformat()}")
            # Check of voordeel groot genoeg is
            if advice and "voordeel:" in advice:
                perc = float(advice.split("voordeel:")[1].split("%")[0].strip())
                if perc >= args.big_advantage:
                    print(f"⚡ Groot voordeel ({perc}%) → override cooldown toegestaan")
                else:
                    print(f"❌ Wissel geblokkeerd: voordeel {perc}% te laag (min {args.big_advantage}%)")
                    exit(1)
            else:
                print("❌ Wissel geblokkeerd: geen voordeel gevonden in adviesregel")
                exit(1)

    print("✅ Wissel toegestaan (geen cooldown actief of override)")


if __name__ == "__main__":
    main()

