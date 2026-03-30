#!/usr/bin/env python3
"""
Trade Advisor — combineert pipeline output + Kraken balans → trade voorstel.

Logica:
1. Lees regime, top coins, dips
2. Check huidige Kraken positie
3. Bepaal of actie nodig is
4. Stuur voorstel naar Telegram met JA/NEE
5. Bij JA → voer trade uit via Kraken

Standalone: python3 scripts/trade_advisor.py --reports-dir data/reports
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Zorg dat project root in sys.path staat
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.kraken import get_balance, find_usd_pair, plan_switch, execute_switch, get_ticker
from src.notify import send_message, send_trade_proposal, send_trade_result, check_confirmation
from src.state import (load_position, save_position, clear_position,
                       is_cooldown_active, should_switch, hours_since_entry)


# ---------------------------------------------------------------------------
# Bepaal huidige positie uit Kraken balans
# ---------------------------------------------------------------------------

# Assets die we negeren (stablecoins, dust)
IGNORE_ASSETS = {"ZUSD", "USD", "ZEUR", "EUR", "USDC", "USDG", "USDT"}
DUST_THRESHOLD_USD = 5.0  # onder $5 = stof


def get_current_position() -> dict:
    """
    Bepaal de grootste niet-stablecoin positie op Kraken.
    Retourneert {symbol, amount, est_usd, asset_key} of None.
    """
    balances = get_balance()
    positions = []

    for asset, amount in balances.items():
        if asset in IGNORE_ASSETS or amount <= 0:
            continue

        # Probeer USD waarde te schatten
        # Kraken asset namen: XXBT=BTC, XETH=ETH, XXDG=DOGE, etc.
        symbol = asset.replace("X", "").replace("Z", "")
        if asset == "XXBT":
            symbol = "BTC"
        elif asset == "XETH":
            symbol = "ETH"
        elif asset == "XXDG":
            symbol = "DOGE"
        elif asset == "XXRP":
            symbol = "XRP"

        pair = find_usd_pair(symbol)
        if not pair:
            continue

        try:
            ticker = get_ticker(pair)
            est_usd = amount * ticker.get("last", 0)
        except Exception:
            est_usd = 0

        if est_usd >= DUST_THRESHOLD_USD:
            positions.append({
                "symbol": symbol,
                "asset_key": asset,
                "amount": amount,
                "est_usd": round(est_usd, 2),
            })

    if not positions:
        return None

    # Grootste positie
    positions.sort(key=lambda x: x["est_usd"], reverse=True)
    return positions[0]


# ---------------------------------------------------------------------------
# Bepaal welke actie nodig is
# ---------------------------------------------------------------------------

def determine_action(reports_dir: Path) -> dict:
    """
    Analyseer pipeline output + Kraken positie → actie bepalen.

    Returns dict met:
        action: 'HOLD' | 'SELL_TO_STABLE' | 'SWITCH' | 'BUY'
        reason: uitleg
        current: huidige positie
        target: doel coin (bij SWITCH/BUY)
    """
    # 1. Lees regime
    md_path = reports_dir / "top5_latest.md"
    regime = "UNKNOWN"
    if md_path.exists():
        for line in md_path.read_text().splitlines():
            if "RISK_ON" in line and "Market regime" in line:
                regime = "RISK_ON"
            elif "CAUTIOUS" in line and "Market regime" in line:
                regime = "CAUTIOUS"
            elif "RISK_OFF" in line and "Market regime" in line:
                regime = "RISK_OFF"

    # 2. Lees top coin
    alloc_path = reports_dir / "allocation_latest.json"
    target_coin = None
    if alloc_path.exists():
        alloc = json.loads(alloc_path.read_text())
        allocation = alloc.get("allocation", {})
        if allocation:
            target_coin = max(allocation, key=allocation.get)

    # 3. Lees dip kansen
    dips_path = reports_dir / "dips_latest.csv"
    best_dip = None
    if dips_path.exists():
        import csv
        with open(dips_path) as f:
            rows = list(csv.DictReader(f))
        if rows:
            top_dip = rows[0]
            if float(top_dip.get("dip_score", 0)) >= 0.7:
                best_dip = top_dip

    # 4. Huidige Kraken positie
    current = get_current_position()
    usd_balance = 0
    for asset, amount in get_balance().items():
        if asset in ("ZUSD", "USD"):
            usd_balance = amount

    # 5. Beslislogica
    result = {
        "regime": regime,
        "current": current,
        "target_coin": target_coin,
        "best_dip": best_dip,
        "usd_available": round(usd_balance, 2),
    }

    # 6. Check positie-state (wanneer zijn we ingestapt?)
    position = load_position()
    hours_in = hours_since_entry()
    cooldown = is_cooldown_active()

    result["position_state"] = position
    result["hours_in_position"] = round(hours_in, 1) if hours_in else None
    result["cooldown_active"] = cooldown

    # ===== RISK_OFF → alles naar stablecoin (cooldown negeren, veiligheid eerst) =====
    if regime == "RISK_OFF":
        if current and current["est_usd"] > DUST_THRESHOLD_USD:
            result["action"] = "SELL_TO_STABLE"
            result["reason"] = (
                f"Regime is RISK_OFF. Je hebt {current['amount']:.4f} {current['symbol']} "
                f"(~${current['est_usd']:.2f}). Advies: verkoop naar USD."
            )
        else:
            result["action"] = "HOLD"
            result["reason"] = "Regime is RISK_OFF. Je zit al in stablecoins. Geen actie nodig."
        return result

    # ===== RISK_ON of CAUTIOUS =====

    # Scores ophalen voor cooldown-check
    scores_path = reports_dir / "scores_latest.csv"
    current_score = 0.0
    target_score = 0.0
    if scores_path.exists():
        import csv
        with open(scores_path) as f:
            for row in csv.DictReader(f):
                sym = row.get("symbol", "").upper()
                score = float(row.get("score", 0))
                if current and sym == current["symbol"].upper():
                    current_score = score
                if target_coin and sym == target_coin.upper():
                    target_score = score

    # Zit je al in de top coin?
    if current and target_coin:
        if current["symbol"].upper() == target_coin.upper():
            result["action"] = "HOLD"
            result["reason"] = (
                f"Regime is {regime}. Je zit al in {current['symbol']} "
                f"(top coin, score {target_score*100:.1f}%). Geen actie nodig."
            )
            return result

        # Andere coin → check of switchen zinvol is (cooldown + voordeel)
        switch = should_switch(current_score, target_score)
        result["switch_analysis"] = switch

        if switch["switch"]:
            result["action"] = "SWITCH"
            result["target"] = target_coin
            result["reason"] = (
                f"Regime is {regime}. {switch['reason']} "
                f"Switch {current['symbol']} → {target_coin}."
            )
        else:
            result["action"] = "HOLD"
            result["reason"] = (
                f"Regime is {regime}. Blijf in {current['symbol']}. "
                f"{switch['reason']}"
            )

    elif not current and usd_balance > DUST_THRESHOLD_USD and target_coin:
        result["action"] = "BUY"
        result["target"] = target_coin
        result["reason"] = (
            f"Regime is {regime}. Je hebt ${usd_balance:.2f} beschikbaar. "
            f"Pipeline adviseert {target_coin} (score {target_score*100:.1f}%)."
        )
    else:
        result["action"] = "HOLD"
        result["reason"] = f"Regime is {regime}. Geen duidelijke actie."

    return result


# ---------------------------------------------------------------------------
# Hoofdflow
# ---------------------------------------------------------------------------

def run_advisor(reports_dir: Path, auto_execute: bool = False) -> None:
    """
    Volledige advisor flow:
    1. Analyseer situatie
    2. Stuur voorstel naar Telegram
    3. Wacht op bevestiging
    4. Voer uit bij JA
    """
    print("[Advisor] Analyseer situatie...")
    action = determine_action(reports_dir)

    print(f"[Advisor] Regime: {action['regime']}")
    print(f"[Advisor] Actie: {action['action']}")
    print(f"[Advisor] Reden: {action['reason']}")

    if action["action"] == "HOLD":
        # Stuur alleen bericht als het regime relevant info bevat
        send_message(f"📊 {action['reason']}")
        return

    if action["action"] == "SELL_TO_STABLE":
        current = action["current"]
        # Plan de verkoop
        pair = find_usd_pair(current["symbol"])
        if not pair:
            send_message(f"❌ Geen USD pair gevonden voor {current['symbol']}")
            return

        msg = (
            f"🔴 <b>VERKOOP Advies (RISK_OFF)</b>\n\n"
            f"Verkoop {current['amount']:.4f} <b>{current['symbol']}</b> "
            f"(~${current['est_usd']:.2f}) naar USD\n"
            f"Fee: ~${current['est_usd'] * 0.0026:.2f}\n\n"
            f"Stuur <b>JA</b> om te verkopen of <b>NEE</b> om te houden."
        )
        send_message(msg)

        if auto_execute:
            return  # in GitHub Actions wachten we niet op antwoord

        # Wacht op bevestiging
        print("[Advisor] Wacht op Telegram bevestiging (5 min)...")
        response = check_confirmation(timeout_seconds=300)
        if response in ("JA", "YES"):
            from src.kraken import place_market_order
            result = place_market_order(pair, "sell", current["amount"])
            clear_position()  # Positie gewist → in stablecoins
            send_trade_result({
                "status": "COMPLETED",
                "sold": f"{current['amount']:.4f} {current['symbol']}",
                "bought": "USD",
                "sell_txids": result.get("txid", []),
                "buy_txids": [],
            })
        else:
            send_message("❌ Trade geannuleerd.")

    elif action["action"] in ("SWITCH", "BUY"):
        target = action.get("target")
        current = action.get("current")

        if action["action"] == "SWITCH" and current:
            plan = plan_switch(current["symbol"], target)
            if "error" in plan:
                send_message(f"❌ Trade planning mislukt: {plan['error']}")
                return

            # Voeg cooldown info toe aan voorstel
            hours = action.get("hours_in_position")
            if hours:
                cooldown_note = f"\n⏱ In {current['symbol']} sinds {hours:.0f}h geleden"
                plan["summary"] += cooldown_note

            send_trade_proposal(plan)

            if auto_execute:
                return

            print("[Advisor] Wacht op Telegram bevestiging (5 min)...")
            response = check_confirmation(timeout_seconds=300)
            if response in ("JA", "YES"):
                result = execute_switch(current["symbol"], target)
                if result.get("status") == "COMPLETED":
                    # Update state: we zitten nu in target
                    ticker = get_ticker(find_usd_pair(target) or "")
                    save_position(target, ticker.get("last", 0),
                                  plan["step2_buy"].get("usd_amount", 0),
                                  source="pipeline")
                send_trade_result(result)
            else:
                send_message("❌ Trade geannuleerd.")

        elif action["action"] == "BUY":
            usd = action["usd_available"]
            pair = find_usd_pair(target)
            if not pair:
                send_message(f"❌ Geen USD pair voor {target}")
                return

            from src.kraken import estimate_trade
            est = estimate_trade(pair, "buy", usd)

            msg = (
                f"🟢 <b>KOOP Advies ({action['regime']})</b>\n\n"
                f"Koop <b>{target}</b> met ${usd:.2f}\n"
                f"Geschat: ~{est.get('est_coins', '?'):.4f} {target}\n"
                f"Prijs: ${est.get('price', '?'):.4f}\n"
                f"Fee: ~${est.get('fee_usd', '?'):.2f}\n\n"
                f"Stuur <b>JA</b> om te kopen of <b>NEE</b> om te wachten."
            )
            send_message(msg)

            if auto_execute:
                return

            print("[Advisor] Wacht op Telegram bevestiging (5 min)...")
            response = check_confirmation(timeout_seconds=300)
            if response in ("JA", "YES"):
                from src.kraken import place_market_order
                result = place_market_order(pair, "buy", usd)
                # Update state: we zitten nu in target
                ticker = get_ticker(pair)
                save_position(target, ticker.get("last", 0), usd,
                              source="dip_finder" if action.get("best_dip") else "pipeline")
                send_trade_result({
                    "status": "COMPLETED",
                    "sold": f"${usd:.2f} USD",
                    "bought": f"{target}",
                    "sell_txids": [],
                    "buy_txids": result.get("txid", []),
                })
            else:
                send_message("❌ Trade geannuleerd.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Trade Advisor")
    ap.add_argument("--reports-dir", type=str, default="data/reports")
    ap.add_argument("--propose-only", action="store_true",
                    help="Stuur alleen voorstel, wacht niet op bevestiging")
    args = ap.parse_args()

    run_advisor(Path(args.reports_dir), auto_execute=args.propose_only)
