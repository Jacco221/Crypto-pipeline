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

from src.kraken import get_balance, find_usd_pair, plan_switch, execute_switch, get_ticker, verify_position
from src.notify import send_message, send_trade_proposal, send_trade_result, check_confirmation
from src.state import (load_position, save_position, clear_position,
                       load_positions, save_positions, clear_positions,
                       is_cooldown_active, should_switch, hours_since_entry,
                       check_stop_loss, check_take_profit, update_peak_price,
                       get_kraken_sl_price)
from src.trade_log import log_trade


# ---------------------------------------------------------------------------
# Bepaal huidige positie uit Kraken balans
# ---------------------------------------------------------------------------

# Assets die we negeren (stablecoins, dust)
IGNORE_ASSETS = {"ZUSD", "USD", "ZEUR", "EUR", "USDC", "USDG", "USDT"}
DUST_THRESHOLD_USD = 5.0  # onder $5 = stof


def _asset_to_symbol(asset: str) -> str:
    """
    Converteer Kraken asset naam naar coin symbol.
    Valideert via find_usd_pair() zodat ook onbekende X/Z-coins correct werken.
    """
    # Bekende Kraken-specifieke namen eerst
    known = {
        "XXBT": "BTC", "XETH": "ETH", "XXDG": "DOGE",
        "XXRP": "XRP", "XLTC": "LTC", "XXLM": "XLM",
        "XZEC": "ZEC", "XXMR": "XMR",
    }
    if asset in known:
        return known[asset]

    # Probeer originele naam eerst (bijv. XPL, ZEC)
    if find_usd_pair(asset):
        return asset

    # Probeer zonder leading X (bijv. XXBT → XBT, maar XPL blijft XPL via bovenstaande)
    if len(asset) > 3 and asset.startswith("X"):
        stripped = asset[1:]
        if find_usd_pair(stripped):
            return stripped

    # Fallback: originele naam
    return asset


def get_all_positions() -> list:
    """
    Geeft alle niet-stablecoin posities op Kraken (voor multi-coin portfolio).
    """
    balances = get_balance()
    positions = []

    for asset, amount in balances.items():
        if asset in IGNORE_ASSETS or amount <= 0:
            continue

        symbol = _asset_to_symbol(asset)

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

        symbol = _asset_to_symbol(asset)

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
        # Bij CAUTIOUS: gebruik slechts 50% van beschikbaar USD
        if regime == "CAUTIOUS":
            invest_usd = usd_balance * 0.5
        else:
            invest_usd = usd_balance

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
            result["invest_usd"] = invest_usd
            result["reason"] = (
                f"Regime is {regime}. Inzet: ${invest_usd:.2f} "
                f"({'50% van ' + str(round(usd_balance)) + ' — cautious' if regime == 'CAUTIOUS' else 'volledig'})."
                f" {buy_reason}"
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
    Volledige advisor flow — volledig automatisch:
    1. Analyseer situatie
    2. Voer trade direct uit
    3. Stuur bevestiging naar Telegram
    """
    print("[Advisor] Analyseer situatie...")
    action = determine_action(reports_dir)

    print(f"[Advisor] Regime: {action['regime']}")
    print(f"[Advisor] Actie: {action['action']}")
    print(f"[Advisor] Reden: {action['reason']}")

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

    # ── STOP-LOSS / TAKE-PROFIT ──────────────────────────────────────────────
    if action["action"] in ("STOP_LOSS", "TAKE_PROFIT"):
        current = action["current"]
        pair = find_usd_pair(current["symbol"])
        if not pair:
            send_message(f"❌ Geen USD pair voor {current['symbol']}")
            return

        emoji = "🛑" if action["action"] == "STOP_LOSS" else "🎯"
        label = "STOP-LOSS" if action["action"] == "STOP_LOSS" else "TAKE-PROFIT"
        current_price = action.get("current_price", 0)

        from src.kraken import place_market_order
        result = place_market_order(pair, "sell", current["amount"])
        position = load_position()
        entry_price = position.get("entry_price") if position else None
        entry_usd = position.get("entry_usd", current["est_usd"]) if position else current["est_usd"]
        pnl_pct = ((current_price - entry_price) / entry_price * 100) if entry_price and current_price else None
        pnl_usd = (current["est_usd"] - entry_usd) if entry_usd else None
        log_trade(
            action=action["action"],
            symbol=current["symbol"],
            price=current_price,
            amount_usd=current["est_usd"],
            pnl_pct=pnl_pct,
            pnl_usd=pnl_usd,
            entry_price=entry_price,
            source=label.lower().replace("-", "_"),
            txids=result.get("txid", []),
        )
        clear_position()
        pnl_str = f" ({pnl_pct:+.1f}%)" if pnl_pct is not None else ""
        send_message(
            f"{regime_header}\n\n"
            f"{emoji} <b>{label} uitgevoerd!</b>\n\n"
            f"{action['reason']}\n\n"
            f"✅ Verkocht: {current['amount']:.4f} <b>{current['symbol']}</b> "
            f"(~${current['est_usd']:.2f}){pnl_str} → USD\n"
            f"TX: {', '.join(result.get('txid', []))}"
        )
        return

    # ── HOLD ────────────────────────────────────────────────────────────────
    if action["action"] == "HOLD":
        dip_info = ""
        if action.get("dip_reason"):
            dip_reason = action["dip_reason"]
            dip_score = float(action.get("best_dip", {}).get("dip_score", 0)) if action.get("best_dip") else 0

            if regime == "RISK_OFF":
                dip_info = (
                    f"\n\n🔔 Dip gesignaleerd: {dip_reason}\n"
                    f"❌ Geen actie — regime is RISK_OFF (wacht op CAUTIOUS/RISK_ON)"
                )
            elif dip_score >= 0.8:
                dip_info = (
                    f"\n\n🔔 Dip gesignaleerd: {dip_reason}\n"
                    f"⏳ Geen actie — al in beste coin of cooldown actief"
                )
            else:
                dip_info = (
                    f"\n\n🔔 Dip gesignaleerd: {dip_reason}\n"
                    f"❌ Geen actie — score {dip_score:.2f} onder drempel 0.80 voor switch"
                )

        send_message(f"{regime_header}\n\n📊 {action['reason']}{pnl_text}{dip_info}")
        return

    # ── SELL TO STABLE ───────────────────────────────────────────────────────
    if action["action"] == "SELL_TO_STABLE":
        all_pos = get_all_positions()
        if not all_pos:
            send_message(f"{regime_header}\n\nGeen posities om te verkopen.")
            return

        from src.kraken import place_market_order
        all_txids = []
        sold_parts = []
        saved_positions = load_positions()
        pos_by_sym = {p["symbol"].upper(): p for p in saved_positions}
        for p in all_pos:
            pair = find_usd_pair(p["symbol"])
            if not pair:
                continue
            ticker = get_ticker(pair)
            exit_price = ticker.get("last", 0)
            result_order = place_market_order(pair, "sell", p["amount"])
            all_txids.extend(result_order.get("txid", []))
            sold_parts.append(f"{p['amount']:.4f} {p['symbol']}")
            saved = pos_by_sym.get(p["symbol"].upper(), {})
            entry_price = saved.get("entry_price")
            entry_usd = saved.get("entry_usd", p["est_usd"])
            pnl_pct = ((exit_price - entry_price) / entry_price * 100) if entry_price else None
            pnl_usd = (p["est_usd"] - entry_usd) if entry_usd else None
            log_trade(
                action="SELL",
                symbol=p["symbol"],
                price=exit_price,
                amount_usd=p["est_usd"],
                pnl_pct=pnl_pct,
                pnl_usd=pnl_usd,
                entry_price=entry_price,
                source="sell_to_stable",
                txids=result_order.get("txid", []),
            )
        clear_positions()
        total_usd = sum(p["est_usd"] for p in all_pos)
        send_message(
            f"{regime_header}\n\n"
            f"🔴 <b>Automatisch verkocht naar USD</b>\n\n"
            f"Verkocht: {' + '.join(sold_parts)}\n"
            f"Totaal: ~${total_usd:.2f}\n"
            f"TX: {', '.join(all_txids)}"
        )
        return

    # ── SWITCH / BUY ─────────────────────────────────────────────────────────
    if action["action"] in ("SWITCH", "BUY"):
        target = action.get("target")
        current = action.get("current")

        if action["action"] == "SWITCH" and current:
            plan = plan_switch(current["symbol"], target)
            if "error" in plan:
                send_message(f"❌ Trade planning mislukt: {plan['error']}")
                return

            # Bij CAUTIOUS: max 50% van totaal portfolio in nieuwe coin
            total_portfolio_usd = current["est_usd"] + action["usd_available"]
            max_usd = (total_portfolio_usd * 0.5) if regime == "CAUTIOUS" else None
            result = execute_switch(current["symbol"], target, max_usd=max_usd)
            if result.get("status") == "COMPLETED":
                ticker = get_ticker(find_usd_pair(target) or "")
                entry_price_new = ticker.get("last", 0)
                usd_amount = plan["step2_buy"].get("usd_amount", 0)
                old_pos = load_position()
                old_entry = old_pos.get("entry_price") if old_pos else None
                old_usd = old_pos.get("entry_usd", current["est_usd"]) if old_pos else current["est_usd"]
                sell_price = plan.get("step1_sell", {}).get("price", 0)
                pnl_pct = ((sell_price - old_entry) / old_entry * 100) if old_entry and sell_price else None
                pnl_usd = (current["est_usd"] - old_usd) if old_usd else None
                log_trade(
                    action="SELL", symbol=current["symbol"],
                    price=sell_price or 0, amount_usd=current["est_usd"],
                    pnl_pct=pnl_pct, pnl_usd=pnl_usd, entry_price=old_entry,
                    source="switch", txids=result.get("sell_txids", []),
                )
                log_trade(
                    action="BUY", symbol=target,
                    price=entry_price_new, amount_usd=usd_amount,
                    source="switch", txids=result.get("buy_txids", []),
                )
                save_position(target, entry_price_new, usd_amount, source="pipeline")
                pnl_str = f" ({pnl_pct:+.1f}%)" if pnl_pct is not None else ""
                send_message(
                    f"{regime_header}\n\n"
                    f"💱 <b>Switch uitgevoerd!</b>\n\n"
                    f"Verkocht: <b>{current['symbol']}</b>{pnl_str}\n"
                    f"Gekocht: <b>{target}</b> @ ${entry_price_new:.4f}\n"
                    f"Bedrag: ${usd_amount:.2f}"
                )
            else:
                send_message(f"❌ Switch mislukt: {result.get('error', '?')}")
            return

        if action["action"] == "BUY":
            usd = action.get("invest_usd", action["usd_available"])
            alloc = action.get("alloc_decision", {})
            is_diversify = (
                alloc.get("decision") == "DIVERSIFY"
                and len(alloc.get("allocation", {})) >= 2
                and regime == "RISK_ON"  # Bij CAUTIOUS: altijd 1 coin (50% budget)
            )

            if is_diversify:
                # ── Diversificatie: koop 2 coins ──
                coins = list(alloc["allocation"].items())
                from src.kraken import place_market_order
                new_positions = []
                all_txids = []
                bought_parts = []
                failed_parts = []
                for sym, weight in coins:
                    usd_part = usd * weight
                    pair_part = find_usd_pair(sym)
                    if not pair_part:
                        failed_parts.append(f"{sym} (geen pair)")
                        continue
                    order_err = None
                    result_order = {}
                    try:
                        result_order = place_market_order(pair_part, "buy", usd_part)
                    except Exception as e:
                        order_err = str(e)

                    # Verificeer werkelijke balans op Kraken
                    verif = verify_position(sym)
                    if verif["confirmed"]:
                        entry_price = verif["price"]
                        actual_usd = verif["est_usd"]
                        new_positions.append({
                            "symbol": sym,
                            "entry_price": entry_price,
                            "entry_usd": actual_usd,
                            "peak_price": entry_price,
                            "source": "pipeline_diversify",
                        })
                        all_txids.extend(result_order.get("txid", []))
                        bought_parts.append(f"✅ <b>{sym}</b> ~${actual_usd:.2f} @ ${entry_price:.4f}")
                        log_trade(
                            action="BUY", symbol=sym, price=entry_price,
                            amount_usd=actual_usd, source="pipeline_diversify",
                            txids=result_order.get("txid", []),
                        )
                    else:
                        failed_parts.append(f"❌ {sym}: {order_err or 'niet gevonden op Kraken'}")

                if new_positions:
                    save_positions(new_positions)
                fail_text = ("\n\n⚠️ Mislukt:\n" + "\n".join(failed_parts)) if failed_parts else ""
                send_message(
                    f"{regime_header}\n\n"
                    f"🟢 <b>Diversificatie aankopen bevestigd</b>\n\n"
                    + "\n".join(bought_parts) +
                    fail_text +
                    f"\n\nTotaal ingezet: ${usd:.2f}"
                )
                return

            # ── Enkele coin koop ──────────────────────────────────────────────
            pair = find_usd_pair(target)
            if not pair:
                send_message(f"❌ Geen USD pair voor {target}")
                return

            from src.kraken import place_market_order
            order_error = None
            result_order = {}
            try:
                result_order = place_market_order(pair, "buy", usd)
            except Exception as e:
                order_error = str(e)

            # Verificeer altijd de werkelijke Kraken balans — ongeacht order response
            verification = verify_position(target)
            source = "dip_finder" if action.get("best_dip") else "pipeline"

            if verification["confirmed"]:
                entry_price = verification["price"]
                actual_usd = verification["est_usd"]
                actual_amount = verification["amount"]
                save_positions([{
                    "symbol": target,
                    "entry_price": entry_price,
                    "entry_usd": actual_usd,
                    "peak_price": entry_price,
                    "source": source,
                }])
                log_trade(
                    action="BUY", symbol=target, price=entry_price,
                    amount_usd=actual_usd, source=source,
                    txids=result_order.get("txid", []),
                )

                # Plaats stop-loss order op Kraken (-15% van entry)
                sl_price = round(entry_price * 0.85, 6)
                sl_note = ""
                try:
                    from src.kraken import place_stop_loss_order
                    place_stop_loss_order(pair, actual_amount, sl_price)
                    sl_note = f"\n🛑 Stop-loss geplaatst op Kraken: ${sl_price:.4f} (-15%)"
                except Exception as e:
                    sl_note = f"\n⚠️ Stop-loss plaatsen mislukt: {e}"

                dip_note = "\n📉 Bron: Dip Finder" if action.get("best_dip") and target == action.get("dip_target") else ""
                warn = f"\n⚠️ Order response: {order_error}" if order_error else ""
                send_message(
                    f"{regime_header}\n\n"
                    f"🟢 <b>Aankoop bevestigd op Kraken!</b>\n\n"
                    f"✅ Gekocht: <b>{target}</b> @ ${entry_price:.4f}\n"
                    f"Bedrag: ~${actual_usd:.2f}{dip_note}{sl_note}{warn}\n"
                    f"TX: {', '.join(result_order.get('txid', []))}"
                )
            else:
                # Positie staat NIET op Kraken
                err = order_error or verification.get("error", "onbekend")
                send_message(
                    f"{regime_header}\n\n"
                    f"❌ <b>Aankoop mislukt — verificatie gefaald</b>\n\n"
                    f"Target: <b>{target}</b>\n"
                    f"Reden: {err}\n\n"
                    f"Geen positie opgeslagen. Controleer je Kraken saldo."
                )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Trade Advisor")
    ap.add_argument("--reports-dir", type=str, default="data/reports")
    args = ap.parse_args()

    run_advisor(Path(args.reports_dir))
