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
                       load_positions, save_positions, clear_positions,
                       is_cooldown_active, should_switch, hours_since_entry,
                       check_stop_loss, check_take_profit, update_peak_price,
                       get_kraken_sl_price)


# ---------------------------------------------------------------------------
# Bepaal huidige positie uit Kraken balans
# ---------------------------------------------------------------------------

# Assets die we negeren (stablecoins, dust)
IGNORE_ASSETS = {"ZUSD", "USD", "ZEUR", "EUR", "USDC", "USDG", "USDT"}
DUST_THRESHOLD_USD = 5.0  # onder $5 = stof


def get_all_positions() -> list:
    """
    Geeft alle niet-stablecoin posities op Kraken (voor multi-coin portfolio).
    """
    balances = get_balance()
    positions = []

    for asset, amount in balances.items():
        if asset in IGNORE_ASSETS or amount <= 0:
            continue

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

    positions.sort(key=lambda x: x["est_usd"], reverse=True)
    return positions


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
    # 1. Lees regime — eerst uit rapport, dan uit regime JSON, anders live berekenen
    regime = "UNKNOWN"

    # Poging 1: uit top5 rapport
    md_path = reports_dir / "top5_latest.md"
    if md_path.exists():
        for line in md_path.read_text().splitlines():
            if "RISK_ON" in line and "Market regime" in line:
                regime = "RISK_ON"
            elif "CAUTIOUS" in line and "Market regime" in line:
                regime = "CAUTIOUS"
            elif "RISK_OFF" in line and "Market regime" in line:
                regime = "RISK_OFF"

    # Poging 2: uit regime JSON (scanner workflow)
    if regime == "UNKNOWN":
        regime_path = reports_dir / "regime_latest.json"
        if regime_path.exists():
            regime_data = json.loads(regime_path.read_text())
            regime = regime_data.get("regime", "UNKNOWN")

    # Poging 3: live berekenen als fallback
    if regime == "UNKNOWN":
        try:
            from src.market_regime import determine_market_regime
            regime_data = determine_market_regime()
            regime = regime_data.get("regime", "RISK_OFF")
        except Exception:
            regime = "RISK_OFF"  # fail-safe: niet handelen

    # 2. Lees top coin + allocatie-beslissing
    alloc_path = reports_dir / "allocation_latest.json"
    target_coin = None
    alloc_decision = {}
    if alloc_path.exists():
        alloc_decision = json.loads(alloc_path.read_text())
        allocation = alloc_decision.get("allocation", {})
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
        "alloc_decision": alloc_decision,
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

    # ===== TP/SL CHECK — HOOGSTE PRIORITEIT (voor regime-check) =====
    if current and current["est_usd"] > DUST_THRESHOLD_USD and position and position.get("entry_price"):
        pair = find_usd_pair(current["symbol"])
        if pair:
            try:
                ticker = get_ticker(pair)
                current_price = ticker.get("last", 0)

                # Update piekprijs
                update_peak_price(current_price)

                # Stop-loss check (-15%)
                sl = check_stop_loss(current_price)
                if sl["triggered"]:
                    result["action"] = "STOP_LOSS"
                    result["reason"] = sl["reason"]
                    result["current_price"] = current_price
                    return result

                # Take-profit check (trailing stop)
                tp = check_take_profit(current_price)
                if tp["triggered"]:
                    result["action"] = "TAKE_PROFIT"
                    result["reason"] = tp["reason"]
                    result["current_price"] = current_price
                    return result

                # Voeg P&L info toe aan result voor berichten
                result["pnl_info"] = {
                    "sl": sl,
                    "tp": tp,
                    "current_price": current_price,
                }
            except Exception:
                pass

    # ===== RISK_OFF of UNKNOWN → alles naar stablecoin (veiligheid eerst) =====
    if regime in ("RISK_OFF", "UNKNOWN"):
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

    # ---- DIP ANALYSE meewegen ----
    # Als er een sterke dip is die beter scoort dan de pipeline top coin,
    # gebruik die als target (contraire instap).
    dip_target = None
    dip_reason = ""
    if best_dip:
        dip_sym = best_dip.get("symbol", "").upper()
        dip_score = float(best_dip.get("dip_score", 0))
        dip_7d = best_dip.get("chg_7d_%", "?")
        dip_24h = best_dip.get("chg_24h_%", "?")

        # Dip is interessant als:
        # 1. Score >= 0.7 (al gefilterd)
        # 2. Het niet je huidige coin is
        # 3. Het niet de pipeline top coin is (anders dubbel)
        is_current = current and dip_sym == current["symbol"].upper()
        is_target = target_coin and dip_sym == target_coin.upper()

        if not is_current and not is_target and dip_score >= 0.7:
            dip_target = dip_sym
            dip_reason = (
                f"Dip-kans: {dip_sym} (dip score {dip_score:.2f}, "
                f"7d: {dip_7d}%, 24h: {dip_24h}%) — "
                f"sterke daling zonder tekenen van slecht nieuws."
            )
        elif is_current:
            # Je zit al in de dip-coin — dat is goed, houd vast
            dip_reason = (
                f"Je zit al in dip-kans {dip_sym} "
                f"(dip score {dip_score:.2f}). Goed — houd vast."
            )

    result["dip_target"] = dip_target
    result["dip_reason"] = dip_reason

    # ---- BESLISLOGICA ----

    # Zit je al in de top coin?
    if current and target_coin:
        if current["symbol"].upper() == target_coin.upper():
            hold_reason = (
                f"Regime is {regime}. Je zit al in {current['symbol']} "
                f"(top coin, score {target_score*100:.1f}%)."
            )
            # Maar is er een betere dip-kans?
            if dip_target:
                # Check cooldown voor switch naar dip
                switch = should_switch(current_score, 0.8)  # dip = hoge urgentie
                if switch["switch"]:
                    result["action"] = "SWITCH"
                    result["target"] = dip_target
                    result["reason"] = (
                        f"{hold_reason} Maar: {dip_reason} "
                        f"Overweeg switch naar dip-kans."
                    )
                    return result

            result["action"] = "HOLD"
            result["reason"] = hold_reason
            if dip_reason:
                result["reason"] += f" {dip_reason}"
            return result

        # Andere coin → check of switchen zinvol is
        # Kies beste target: pipeline top coin OF dip-kans
        best_target = target_coin
        best_reason = f"Pipeline adviseert {target_coin}"
        if dip_target:
            # Dip-kans krijgt voorrang als dip_score > 0.8
            dip_s = float(best_dip.get("dip_score", 0)) if best_dip else 0
            if dip_s >= 0.8:
                best_target = dip_target
                best_reason = dip_reason

        switch = should_switch(current_score, target_score)
        result["switch_analysis"] = switch

        if switch["switch"]:
            result["action"] = "SWITCH"
            result["target"] = best_target
            result["reason"] = (
                f"Regime is {regime}. {switch['reason']} "
                f"{best_reason}. Switch {current['symbol']} → {best_target}."
            )
        else:
            result["action"] = "HOLD"
            result["reason"] = (
                f"Regime is {regime}. Blijf in {current['symbol']}. "
                f"{switch['reason']}"
            )
            if dip_reason:
                result["reason"] += f" (Dip info: {dip_reason})"

    elif not current and usd_balance > DUST_THRESHOLD_USD:
        # Geen positie, USD beschikbaar → koop pipeline top OF dip-kans
        buy_target = target_coin
        buy_reason = f"Pipeline adviseert {target_coin} (score {target_score*100:.1f}%)"

        if dip_target:
            dip_s = float(best_dip.get("dip_score", 0)) if best_dip else 0
            if dip_s >= 0.8 or not target_coin:
                buy_target = dip_target
                buy_reason = dip_reason

        if buy_target:
            result["action"] = "BUY"
            result["target"] = buy_target
            result["reason"] = (
                f"Regime is {regime}. Je hebt ${usd_balance:.2f} beschikbaar. "
                f"{buy_reason}"
            )
        else:
            result["action"] = "HOLD"
            result["reason"] = f"Regime is {regime}. Geen duidelijk koopadvies."
    else:
        result["action"] = "HOLD"
        result["reason"] = f"Regime is {regime}. Geen duidelijke actie."
        if dip_reason:
            result["reason"] += f" (Dip info: {dip_reason})"

    return result


# ---------------------------------------------------------------------------
# Hoofdflow
# ---------------------------------------------------------------------------

def run_advisor(reports_dir: Path) -> None:
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

    # Regime emoji voor alle berichten
    regime = action["regime"]
    regime_emoji = {"RISK_ON": "🟢", "CAUTIOUS": "🟡", "RISK_OFF": "🔴"}.get(regime, "⚪")
    regime_header = f"{regime_emoji} Regime: <b>{regime}</b>"

    # P&L info toevoegen aan HOLD berichten
    pnl_text = ""
    pnl = action.get("pnl_info")
    if pnl:
        sl_info = pnl["sl"]
        tp_info = pnl["tp"]
        pnl_text = f"\n\n💰 P&L: {sl_info['loss_pct']:+.1f}% | {tp_info['reason']}"

    if action["action"] in ("STOP_LOSS", "TAKE_PROFIT"):
        current = action["current"]
        pair = find_usd_pair(current["symbol"])
        if not pair:
            send_message(f"❌ Geen USD pair voor {current['symbol']}")
            return

        if action["action"] == "STOP_LOSS":
            emoji = "🛑"
            label = "STOP-LOSS"
        else:
            emoji = "🎯"
            label = "TAKE-PROFIT"

        msg = (
            f"{regime_header}\n\n"
            f"{emoji} <b>{label}!</b>\n\n"
            f"{action['reason']}\n\n"
            f"Verkoop {current['amount']:.4f} <b>{current['symbol']}</b> "
            f"(~${current['est_usd']:.2f}) naar USD\n"
            f"Fee: ~${current['est_usd'] * 0.0026:.2f}\n\n"
            f"Stuur <b>JA</b> om te verkopen of <b>NEE</b> om te houden."
        )
        send_message(msg)

        print(f"[Advisor] Wacht op Telegram bevestiging (5 min)...")
        response = check_confirmation(timeout_seconds=300)
        if response in ("JA", "YES"):
            from src.kraken import place_market_order
            result = place_market_order(pair, "sell", current["amount"])
            clear_position()
            send_trade_result({
                "status": "COMPLETED",
                "sold": f"{current['amount']:.4f} {current['symbol']} ({label})",
                "bought": "USD",
                "sell_txids": result.get("txid", []),
                "buy_txids": [],
            })
        else:
            send_message("❌ Trade geannuleerd.")
        return

    if action["action"] == "HOLD":
        dip_info = ""
        if action.get("dip_reason"):
            dip_info = f"\n\n🔔 {action['dip_reason']}"
            if regime == "RISK_OFF":
                dip_info += "\n⚠️ Nog niet instappen — wacht op regime-wissel."

        send_message(f"{regime_header}\n\n📊 {action['reason']}{pnl_text}{dip_info}")
        return

    if action["action"] == "SELL_TO_STABLE":
        # Verkoop ALLE posities (ook bij diversificatie)
        all_pos = get_all_positions()
        if not all_pos:
            send_message(f"{regime_header}\n\nGeen posities om te verkopen.")
            return

        sell_lines = "\n".join(
            f"  {p['amount']:.4f} <b>{p['symbol']}</b> (~${p['est_usd']:.2f})"
            for p in all_pos
        )
        total_usd = sum(p["est_usd"] for p in all_pos)
        total_fee = total_usd * 0.0026 * len(all_pos)

        msg = (
            f"{regime_header}\n\n"
            f"🔴 <b>VERKOOP Advies</b>\n\n"
            f"Verkoop naar USD:\n{sell_lines}\n\n"
            f"Totaal: ~${total_usd:.2f} | Fee: ~${total_fee:.2f}\n\n"
            f"Stuur <b>JA</b> om te verkopen of <b>NEE</b> om te houden."
        )
        send_message(msg)

        print("[Advisor] Wacht op Telegram bevestiging (5 min)...")
        response = check_confirmation(timeout_seconds=300)
        if response in ("JA", "YES"):
            from src.kraken import place_market_order
            all_txids = []
            sold_parts = []
            for p in all_pos:
                pair = find_usd_pair(p["symbol"])
                if not pair:
                    continue
                result_order = place_market_order(pair, "sell", p["amount"])
                all_txids.extend(result_order.get("txid", []))
                sold_parts.append(f"{p['amount']:.4f} {p['symbol']}")
            clear_positions()
            send_trade_result({
                "status": "COMPLETED",
                "sold": " + ".join(sold_parts),
                "bought": "USD",
                "sell_txids": all_txids,
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

            # Voeg regime + cooldown info toe aan voorstel
            plan["regime"] = regime
            plan["regime_header"] = regime_header
            hours = action.get("hours_in_position")
            if hours:
                cooldown_note = f"\n⏱ In {current['symbol']} sinds {hours:.0f}h geleden"
                plan["summary"] += cooldown_note

            send_trade_proposal(plan)

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
            alloc = action.get("alloc_decision", {})
            is_diversify = (
                alloc.get("decision") == "DIVERSIFY"
                and len(alloc.get("allocation", {})) >= 2
            )

            if is_diversify:
                # ── Diversificatie: koop 2 coins ──
                coins = list(alloc["allocation"].items())  # [("CHZ", 0.5), ("ALGO", 0.5)]
                from src.kraken import estimate_trade

                buy_lines = ""
                for sym, weight in coins:
                    usd_part = usd * weight
                    pair_part = find_usd_pair(sym)
                    if pair_part:
                        est = estimate_trade(pair_part, "buy", usd_part)
                        buy_lines += (
                            f"  <b>{sym}</b> {weight*100:.0f}% — "
                            f"${usd_part:.2f} → ~{est.get('est_coins', '?'):.4f} "
                            f"@ ${est.get('price', '?'):.4f}\n"
                        )

                msg = (
                    f"{regime_header}\n\n"
                    f"🟢 <b>KOOP Advies — Diversificatie</b>\n\n"
                    f"Totaal beschikbaar: ${usd:.2f}\n\n"
                    f"{buy_lines}\n"
                    f"Stuur <b>JA</b> om te kopen of <b>NEE</b> om te wachten."
                )
                send_message(msg)

                print("[Advisor] Wacht op Telegram bevestiging (5 min)...")
                response = check_confirmation(timeout_seconds=300)
                if response in ("JA", "YES"):
                    from src.kraken import place_market_order
                    new_positions = []
                    all_txids = []
                    for sym, weight in coins:
                        usd_part = usd * weight
                        pair_part = find_usd_pair(sym)
                        if not pair_part:
                            continue
                        result_order = place_market_order(pair_part, "buy", usd_part)
                        ticker = get_ticker(pair_part)
                        entry_price = ticker.get("last", 0)
                        new_positions.append({
                            "symbol": sym,
                            "entry_price": entry_price,
                            "entry_usd": usd_part,
                            "peak_price": entry_price,
                            "source": "pipeline_diversify",
                        })
                        all_txids.extend(result_order.get("txid", []))

                    save_positions(new_positions)
                    send_trade_result({
                        "status": "COMPLETED",
                        "sold": f"${usd:.2f} USD",
                        "bought": " + ".join(s for s, _ in coins),
                        "sell_txids": [],
                        "buy_txids": all_txids,
                    })
                else:
                    send_message("❌ Trade geannuleerd.")

            else:
                # ── Enkele coin koop (bestaande logica) ──
                pair = find_usd_pair(target)
                if not pair:
                    send_message(f"❌ Geen USD pair voor {target}")
                    return

                from src.kraken import estimate_trade
                est = estimate_trade(pair, "buy", usd)

                dip_note = ""
                if action.get("best_dip") and target == action.get("dip_target"):
                    dip_note = "\n📉 Bron: Dip Finder (contraire instap)"

                msg = (
                    f"{regime_header}\n\n"
                    f"🟢 <b>KOOP Advies</b>\n\n"
                    f"Koop <b>{target}</b> met ${usd:.2f}\n"
                    f"Geschat: ~{est.get('est_coins', '?'):.4f} {target}\n"
                    f"Prijs: ${est.get('price', '?'):.4f}\n"
                    f"Fee: ~${est.get('fee_usd', '?'):.2f}"
                    f"{dip_note}\n\n"
                    f"Stuur <b>JA</b> om te kopen of <b>NEE</b> om te wachten."
                )
                send_message(msg)

                print("[Advisor] Wacht op Telegram bevestiging (5 min)...")
                response = check_confirmation(timeout_seconds=300)
                if response in ("JA", "YES"):
                    from src.kraken import place_market_order
                    result_order = place_market_order(pair, "buy", usd)
                    ticker = get_ticker(pair)
                    save_positions([{
                        "symbol": target,
                        "entry_price": ticker.get("last", 0),
                        "entry_usd": usd,
                        "peak_price": ticker.get("last", 0),
                        "source": "dip_finder" if action.get("best_dip") else "pipeline",
                    }])
                    send_trade_result({
                        "status": "COMPLETED",
                        "sold": f"${usd:.2f} USD",
                        "bought": target,
                        "sell_txids": [],
                        "buy_txids": result_order.get("txid", []),
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
    # Trade advisor wacht altijd op Telegram bevestiging (5 min timeout)
    args = ap.parse_args()

    run_advisor(Path(args.reports_dir))
