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

from src.kraken import get_balance, find_usd_pair, plan_switch, execute_switch, get_ticker, verify_position, update_trailing_stop, place_native_trailing_stop
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
    # BTC → XBT want Kraken gebruikt XBT/USD (niet BTC/USD)
    known = {
        "XXBT": "XBT", "XETH": "ETH", "XXDG": "DOGE",
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
    Inclusief coins zonder USD pair (est_usd=0, unknown_pair=True) zodat
    het systeem ze niet stilzwijgend negeert.
    """
    balances = get_balance()
    positions = []

    for asset, amount in balances.items():
        if asset in IGNORE_ASSETS or amount <= 0:
            continue

        symbol = _asset_to_symbol(asset)

        pair = find_usd_pair(symbol)
        est_usd = 0.0
        unknown_pair = False

        if pair:
            try:
                ticker = get_ticker(pair)
                est_usd = amount * ticker.get("last", 0)
            except Exception:
                est_usd = 0
        else:
            # Geen USD pair gevonden — probeer CoinGecko schatting (fallback)
            # Markeer als unknown zodat de advisor een alert kan sturen
            unknown_pair = True
            print(f"[Advisor] ⚠️ Geen USD pair voor {asset} ({symbol}) — positie gemarkeerd als unknown")

        # Voeg toe als > dust (of als unknown — dan willen we het zeker weten)
        if est_usd >= DUST_THRESHOLD_USD or (unknown_pair and amount > 0.001):
            positions.append({
                "symbol": symbol,
                "asset_key": asset,
                "amount": amount,
                "est_usd": round(est_usd, 2),
                "unknown_pair": unknown_pair,
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
# Portfolio snapshot helpers — voor/na check bij elke trade
# ---------------------------------------------------------------------------

def _portfolio_snapshot() -> dict:
    """
    Neem een momentopname van het huidige Kraken portfolio.
    Gebruikt als voor- en nameting bij elke trade.
    """
    try:
        from src.kraken import get_balance
        balances = get_balance()
        usd = balances.get("ZUSD", balances.get("USD", 0))
        positions = get_all_positions()
        total = round(usd + sum(p["est_usd"] for p in positions), 2)
        return {"usd": round(usd, 2), "positions": positions, "total_usd": total, "ok": True}
    except Exception as e:
        return {"usd": 0, "positions": [], "total_usd": 0, "ok": False, "error": str(e)}


def _snapshot_text(snapshot: dict, label: str = "") -> str:
    """Maak leesbare tekst van een portfolio snapshot."""
    header = f"<b>{label}</b>\n" if label else ""
    lines = []
    for p in snapshot.get("positions", []):
        lines.append(f"  🪙 {p['symbol']}: {p['amount']:.4f} (~${p['est_usd']:.2f})")
    usd = snapshot.get("usd", 0)
    if usd >= 1:
        lines.append(f"  💵 USD: ${usd:.2f}")
    lines.append(f"  📊 Totaal: <b>${snapshot.get('total_usd', 0):.2f}</b>")
    if not snapshot.get("ok"):
        lines.append(f"  ⚠️ Snapshot fout: {snapshot.get('error', '?')}")
    return header + "\n".join(lines)


def _compare_snapshots(before: dict, after: dict) -> str:
    """Vergelijk twee snapshots en toon verschil in portfolio waarde."""
    diff = after.get("total_usd", 0) - before.get("total_usd", 0)
    diff_str = f"{diff:+.2f}"
    return f"  📈 Portfoliowaarde: ${before.get('total_usd',0):.2f} → ${after.get('total_usd',0):.2f} ({diff_str})"


# ---------------------------------------------------------------------------
# Bepaal welke actie nodig is
# ---------------------------------------------------------------------------

def _check_pump_filter(symbol: str, regime: str = "CAUTIOUS",
                       reports_dir: Path = None) -> dict:
    """
    Blokkeer aankoop als een coin te veel is gestegen in 7 dagen.
    Drempel is regime-afhankelijk:
      CAUTIOUS / RISK_OFF → 100%  (strikt: onzekere markt)
      RISK_ON             → 200%  (soepeler: bull markt, grote moves zijn reëler)

    Primaire bron: scores_latest.csv (al beschikbaar, geen extra API call)
    Fallback: CoinGecko API
    Bij twijfel (API fout, geen data): BLOKKEER — veiligheid boven kans.
    """
    max_7d_pct = 200.0 if regime == "RISK_ON" else 100.0
    chg_7d = None

    # Primaire bron: scores_latest.csv — gebruik chg_7d_raw (echte % koerswijziging)
    if reports_dir:
        try:
            import csv as _csv
            scores_path = reports_dir / "scores_latest.csv"
            if scores_path.exists():
                with open(scores_path) as f:
                    for row in _csv.DictReader(f):
                        if row.get("symbol", "").upper() == symbol.upper():
                            raw = row.get("chg_7d_raw", "")
                            if raw not in ("", None):
                                chg_7d = float(raw)
                            break
        except Exception as e:
            print(f"[Advisor] Pump filter scores CSV fout: {e}")

    # Fallback: CoinGecko API
    if chg_7d is None:
        try:
            import requests as _req
            cg_id_map = {
                "RAVE": "ravedao", "CFG": "centrifuge", "XPL": "xenoplasm",
                "BANANAS31": "bananas31", "BTC": "bitcoin", "ETH": "ethereum",
            }
            cg_id = cg_id_map.get(symbol.upper(), symbol.lower())
            r = _req.get(
                f"https://api.coingecko.com/api/v3/coins/{cg_id}",
                params={"localization": "false", "tickers": "false",
                        "community_data": "false"},
                timeout=10,
            )
            if r.status_code == 200:
                data = r.json()
                chg_7d = data.get("market_data", {}).get(
                    "price_change_percentage_7d", None)
        except Exception as e:
            print(f"[Advisor] Pump filter CoinGecko fout voor {symbol}: {e}")

    # Bij twijfel (geen data): blokkeer — veiligheid boven kans
    if chg_7d is None:
        print(f"[Advisor] Pump filter: geen 7d data voor {symbol} — geblokkeerd (veiligheid)")
        return {
            "blocked": True,
            "reason": f"⛔ Pump filter: geen 7d data voor {symbol} — geblokkeerd (veiligheid boven kans)",
            "chg_7d": None,
        }

    if chg_7d >= max_7d_pct:
        return {
            "blocked": True,
            "reason": f"⛔ Pump filter: {symbol} is +{chg_7d:.0f}% in 7 dagen (max {max_7d_pct:.0f}% bij {regime}) — niet instappen na extreme pump",
            "chg_7d": chg_7d,
        }
    return {"blocked": False, "chg_7d": chg_7d}


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

    # 4. Huidige Kraken positie — lees ALTIJD direct van Kraken (niet van state file)
    all_kraken_positions = get_all_positions()
    current = all_kraken_positions[0] if all_kraken_positions else None
    usd_balance = 0
    for asset, amount in get_balance().items():
        if asset in ("ZUSD", "USD"):
            usd_balance = amount

    # Veiligheidscheck: als er al crypto op Kraken staat, nooit extra kopen
    # Dit voorkomt dubbele posities als positions.json leeg/outdated is
    if current and len(all_kraken_positions) > 0:
        # Zet current altijd op de grootste Kraken positie
        current = max(all_kraken_positions, key=lambda x: x["est_usd"])

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
            # Dip coin kan top coin verslaan, maar alleen bij hoge score + RISK_ON
            if dip_target:
                dip_score_val = float(best_dip.get("dip_score", 0)) if best_dip else 0
                if dip_score_val >= 0.85 and regime == "RISK_ON":
                    result["action"] = "SWITCH"
                    result["target"] = dip_target
                    result["reason"] = (
                        f"{hold_reason} Maar: {dip_reason} "
                        f"(score {dip_score_val:.2f} >= 0.85 + RISK_ON → switch)"
                    )
                    return result
                else:
                    why = "score onder 0.85" if dip_score_val < 0.85 else "regime niet RISK_ON"
                    dip_reason += f" (geen actie — {why})"

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

        # positions.json is altijd leeg in GitHub Actions (gitignored).
        # Als we een echte Kraken positie hebben, doe altijd score-vergelijking
        # en nooit "geen positie — vrij om in te stappen" gebruiken als reden.
        switch = should_switch(current_score, target_score)

        # Override: als should_switch zei "geen positie" maar we hebben WEL een
        # Kraken positie, behandel het als een normale score-check zonder cooldown
        if switch.get("reason", "").startswith("Geen huidige positie") and current:
            min_advantage = 5.0  # minstens 5% beter voordat we switchen
            advantage = 0.0
            if current_score > 0:
                advantage = ((target_score - current_score) / current_score) * 100
            if advantage >= min_advantage:
                switch = {"switch": True, "reason": f"Score voordeel {advantage:.1f}% (>= {min_advantage}%)"}
            else:
                switch = {"switch": False, "reason": f"Score voordeel {advantage:.1f}% — te klein om te switchen (min {min_advantage}%)"}

        result["switch_analysis"] = switch

        if switch["switch"]:
            # Pump filter — drempel afhankelijk van regime
            pump = _check_pump_filter(best_target, regime=regime, reports_dir=reports_dir)
            if pump["blocked"]:
                result["action"] = "HOLD"
                result["reason"] = f"Regime is {regime}. {pump['reason']}"
                return result

            # Token unlock check vóór switch
            from src.token_unlocks import check_upcoming_unlocks, unlock_check_text
            unlock = check_upcoming_unlocks(best_target)
            if unlock["risk"] == "BLOCK":
                result["action"] = "HOLD"
                result["reason"] = (
                    f"Regime is {regime}. Switch naar {best_target} geblokkeerd. "
                    f"{unlock['reason']}"
                )
                return result
            unlock_note = f" {unlock_check_text(unlock)}" if unlock["risk"] in ("WARNING", "UNKNOWN") else ""

            result["action"] = "SWITCH"
            result["target"] = best_target
            result["unlock_check"] = unlock
            result["reason"] = (
                f"Regime is {regime}. {switch['reason']} "
                f"{best_reason}. Switch {current['symbol']} → {best_target}.{unlock_note}"
            )
        else:
            result["action"] = "HOLD"
            result["reason"] = (
                f"Regime is {regime}. Blijf in {current['symbol']}. "
                f"{switch['reason']}"
            )
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
            # Pump filter — drempel afhankelijk van regime
            pump = _check_pump_filter(buy_target, regime=regime, reports_dir=reports_dir)
            if pump["blocked"]:
                result["action"] = "HOLD"
                result["reason"] = f"Regime is {regime}. {pump['reason']}"
                return result

            # Token unlock check vóór aankoop
            from src.token_unlocks import check_upcoming_unlocks, unlock_check_text
            unlock = check_upcoming_unlocks(buy_target)
            if unlock["risk"] == "BLOCK":
                result["action"] = "HOLD"
                result["reason"] = (
                    f"Regime is {regime}. Aankoop {buy_target} geblokkeerd. "
                    f"{unlock['reason']}"
                )
                return result
            unlock_note = f" {unlock_check_text(unlock)}" if unlock["risk"] in ("WARNING", "UNKNOWN") else ""

            result["action"] = "BUY"
            result["target"] = buy_target
            result["invest_usd"] = invest_usd
            result["unlock_check"] = unlock
            result["reason"] = (
                f"Regime is {regime}. Inzet: ${invest_usd:.2f} "
                f"({'50% van ' + str(round(usd_balance)) + ' — cautious' if regime == 'CAUTIOUS' else 'volledig'})."
                f" {buy_reason}{unlock_note}"
            )
        else:
            result["action"] = "HOLD"
            result["reason"] = f"Regime is {regime}. Geen duidelijk koopadvies."
    else:
        result["action"] = "HOLD"
        # Geef duidelijkere reden: zit je in een coin, of is er geen data?
        if current:
            result["reason"] = (
                f"Regime is {regime}. Je zit in {current['symbol']} (~${current['est_usd']:.0f}). "
                f"Geen allocation data beschikbaar (scanner run) — HOLD."
            )
        else:
            result["reason"] = f"Regime is {regime}. Geen positie en geen koopadvies — HOLD."
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

    # Controleer eerst of er onbekende posities zijn (geen USD pair)
    # Dit zijn coins die het systeem niet automatisch kan verkopen — stuur direct alert
    all_pos = get_all_positions()
    unknown = [p for p in all_pos if p.get("unknown_pair")]
    if unknown:
        names = ", ".join(p["symbol"] for p in unknown)
        send_message(
            f"⚠️ <b>Onbekende positie(s) gedetecteerd</b>\n\n"
            f"Coins zonder USD pair: <b>{names}</b>\n"
            f"Het systeem kan deze niet automatisch verkopen.\n\n"
            f"👉 Verkoop handmatig via Kraken en trigger daarna 'Correctie Allocatie'."
        )
        print(f"[Advisor] ⚠️ Onbekende posities: {names}")

    # ── DETECTEER UITGEVOERDE NATIVE TRAILING STOP ───────────────────────────
    # Als positions.json een coin heeft maar die staat niet meer op Kraken
    # → Kraken heeft de trailing stop uitgevoerd tussen twee pipeline runs in.
    saved_positions = load_positions()
    for saved in saved_positions:
        sym = saved.get("symbol", "")
        if not sym:
            continue
        still_on_kraken = any(p["symbol"].upper() == sym.upper() for p in all_pos)
        if not still_on_kraken:
            entry_price = saved.get("entry_price", 0)
            entry_usd = saved.get("entry_usd", 0)
            print(f"[Advisor] 🛑 Trailing stop uitgevoerd door Kraken: {sym}")
            send_message(
                f"🛑 <b>Trailing stop uitgevoerd: {sym}</b>\n\n"
                f"Kraken heeft automatisch verkocht terwijl de pipeline niet draaide.\n"
                f"Ingekocht @ ${entry_price:.6f} (~${entry_usd:.2f})\n\n"
                f"💵 Positie is nu USD. Pipeline bepaalt volgende stap."
            )
            log_trade(action="TRAILING_STOP_TRIGGERED", symbol=sym,
                      price=0, amount_usd=entry_usd, entry_price=entry_price,
                      source="kraken_native_trailing_stop")
    # Ruim verlopen posities op uit state
    active_symbols = {p["symbol"].upper() for p in all_pos}
    remaining = [s for s in saved_positions if s.get("symbol", "").upper() in active_symbols]
    if len(remaining) != len(saved_positions):
        save_positions(remaining)

    # ── NATIVE TRAILING STOP CHECK ────────────────────────────────────────────
    # Controleer of er een native trailing-stop op Kraken staat voor elke positie.
    # Als er geen is (bijv. na herstart of handmatige trade), place er een.
    # Als er al een staat: niets doen — Kraken volgt de prijs real-time zelf.
    TRAIL_PCT = 0.20  # 20% trailing offset
    for pos in all_pos:
        if pos.get("unknown_pair"):
            continue
        sym = pos["symbol"]
        price = pos["est_usd"] / pos["amount"] if pos["amount"] > 0 else 0
        if price <= 0:
            continue
        try:
            trail = update_trailing_stop(sym, price, trail_pct=TRAIL_PCT)
            if trail["action"] == "placed":
                new = trail.get("new_stop", 0)
                print(f"[Advisor] 🛑 Trailing stop geplaatst voor {sym}: ${new:.4f} (-{TRAIL_PCT*100:.0f}%)")
                send_message(
                    f"🛑 <b>Trailing stop geplaatst: {sym}</b>\n\n"
                    f"Stop: <b>${new:.4f}</b> (-{TRAIL_PCT*100:.0f}% van ${price:.4f})\n"
                    f"✅ Positie beschermd."
                )
            elif trail["action"] == "updated":
                old = trail.get("old_stop", 0)
                new = trail.get("new_stop", 0)
                print(f"[Advisor] 📈 Trailing stop omhoog {sym}: ${old:.4f} → ${new:.4f}")
                send_message(
                    f"📈 <b>Trailing stop omhoog: {sym}</b>\n\n"
                    f"Prijs gestegen → stop bijgewerkt\n"
                    f"Oud: ${old:.4f} → Nieuw: <b>${new:.4f}</b> (-{TRAIL_PCT*100:.0f}% van ${price:.4f})\n"
                    f"✅ Winst beter beschermd."
                )
            elif trail["action"] == "no_change":
                print(f"[Advisor] ✓ Stop actief voor {sym} @ ${trail.get('existing_stop', 0):.4f}")
        except Exception as e:
            print(f"[Advisor] Trailing stop fout voor {sym}: {e}")

    action = determine_action(reports_dir)

    print(f"[Advisor] Regime: {action['regime']}")
    print(f"[Advisor] Actie: {action['action']}")
    print(f"[Advisor] Reden: {action['reason']}")

    regime = action["regime"]
    regime_emoji = {"RISK_ON": "🟢", "CAUTIOUS": "🟡", "RISK_OFF": "🔴"}.get(regime, "⚪")
    macro_note = action.get("alloc_decision", {}).get("macro_note", "") or ""
    # Haal macro note ook direct op als die er is
    try:
        from src.macro_calendar import macro_note as _macro_note
        _mn = _macro_note()
        if _mn:
            macro_note = _mn
    except Exception:
        pass
    regime_header = f"{regime_emoji} Regime: <b>{regime}</b>"
    if macro_note:
        regime_header += f"\n{macro_note}"

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

        from src.kraken import place_market_order, cancel_all_orders
        try:
            cancel_all_orders()
            import time as _t; _t.sleep(2)
        except Exception as e:
            print(f"[Advisor] Cancel waarschuwing ({label}): {e}")
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

        # ── VOOR-snapshot ──
        snap_before = _portfolio_snapshot()
        send_message(
            f"{regime_header}\n\n"
            f"🔴 <b>Verkoop naar USD gestart</b>\n\n"
            f"<b>Portfolio VOOR:</b>\n{_snapshot_text(snap_before)}"
        )

        from src.kraken import place_market_order, cancel_all_orders
        import time as _time
        try:
            cancel_all_orders()
            _time.sleep(2)
        except Exception as e:
            print(f"[Advisor] Cancel waarschuwing (sell_to_stable): {e}")

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

        # ── NA-snapshot ──
        import time as _time; _time.sleep(3)
        snap_after = _portfolio_snapshot()
        send_message(
            f"{regime_header}\n\n"
            f"🔴 <b>Verkoop voltooid</b>\n\n"
            f"Verkocht: {' + '.join(sold_parts)}\n\n"
            f"<b>Portfolio NA:</b>\n{_snapshot_text(snap_after)}\n\n"
            f"{_compare_snapshots(snap_before, snap_after)}\n"
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

            # ── VOOR-snapshot ──
            snap_before = _portfolio_snapshot()
            total_portfolio_usd = current["est_usd"] + action["usd_available"]
            max_usd = (total_portfolio_usd * 0.5) if regime == "CAUTIOUS" else None
            max_str = f"Max invest: ${max_usd:.2f} (CAUTIOUS 50%)" if max_usd else "Volledig investeren (RISK_ON)"
            send_message(
                f"{regime_header}\n\n"
                f"💱 <b>Switch gestart: {current['symbol']} → {target}</b>\n\n"
                f"<b>Portfolio VOOR:</b>\n{_snapshot_text(snap_before)}\n\n"
                f"📋 {max_str}"
            )

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

                # ── Plaats native trailing stop op nieuwe positie ──
                target_pair = find_usd_pair(target)
                sl_note = ""
                if target_pair and entry_price_new > 0:
                    try:
                        verif = verify_position(target)
                        if verif["confirmed"]:
                            place_native_trailing_stop(target_pair, verif["amount"],
                                                       trail_pct=TRAIL_PCT,
                                                       current_price=entry_price_new)
                            sl_note = f"\n🛑 Trailing stop geplaatst: -{TRAIL_PCT*100:.0f}% van instapprijs"
                        else:
                            sl_note = "\n⚠️ Trailing stop overgeslagen — positie niet geverifieerd"
                    except Exception as e:
                        sl_note = f"\n⚠️ Trailing stop mislukt: {e}"

                # ── NA-snapshot ──
                import time as _time; _time.sleep(3)
                snap_after = _portfolio_snapshot()
                pnl_str = f" ({pnl_pct:+.1f}%)" if pnl_pct is not None else ""
                send_message(
                    f"{regime_header}\n\n"
                    f"✅ <b>Switch voltooid: {current['symbol']} → {target}</b>\n\n"
                    f"Verkocht: <b>{current['symbol']}</b>{pnl_str}\n"
                    f"Gekocht: <b>{target}</b> @ ${entry_price_new:.4f}{sl_note}\n\n"
                    f"<b>Portfolio NA:</b>\n{_snapshot_text(snap_after)}\n\n"
                    f"{_compare_snapshots(snap_before, snap_after)}"
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
                        # Trailing stop per coin
                        ts_note = ""
                        try:
                            place_native_trailing_stop(pair_part, verif["amount"],
                                                       trail_pct=TRAIL_PCT,
                                                       current_price=entry_price)
                            ts_note = f" 🛑-{TRAIL_PCT*100:.0f}%"
                        except Exception as e:
                            ts_note = f" ⚠️TS:{e}"
                        bought_parts.append(f"✅ <b>{sym}</b> ~${actual_usd:.2f} @ ${entry_price:.4f}{ts_note}")
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

            # ── VOOR-snapshot ──
            snap_before = _portfolio_snapshot()
            cautious_str = f" (CAUTIOUS: 50% van ${snap_before['usd']:.2f})" if regime == "CAUTIOUS" else ""
            send_message(
                f"{regime_header}\n\n"
                f"🟢 <b>Aankoop gestart: {target}</b>\n\n"
                f"<b>Portfolio VOOR:</b>\n{_snapshot_text(snap_before)}\n\n"
                f"📋 Inzet: ${usd:.2f}{cautious_str}"
            )

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

                # Plaats native trailing-stop op Kraken (-20% trailing, real-time)
                sl_note = ""
                try:
                    place_native_trailing_stop(pair, actual_amount, trail_pct=TRAIL_PCT, current_price=entry_price)
                    sl_note = f"\n🛑 Trailing stop geplaatst: -{TRAIL_PCT*100:.0f}% van instapprijs"
                except Exception as e:
                    sl_note = f"\n⚠️ Trailing stop plaatsen mislukt: {e}"

                # ── NA-snapshot ──
                import time as _time; _time.sleep(3)
                snap_after = _portfolio_snapshot()
                dip_note = "\n📉 Bron: Dip Finder" if action.get("best_dip") and target == action.get("dip_target") else ""
                warn = f"\n⚠️ Order response: {order_error}" if order_error else ""
                send_message(
                    f"{regime_header}\n\n"
                    f"✅ <b>Aankoop bevestigd op Kraken!</b>\n\n"
                    f"Gekocht: <b>{target}</b> @ ${entry_price:.4f}{dip_note}{sl_note}{warn}\n\n"
                    f"<b>Portfolio NA:</b>\n{_snapshot_text(snap_after)}\n\n"
                    f"{_compare_snapshots(snap_before, snap_after)}\n"
                    f"TX: {', '.join(result_order.get('txid', []))}"
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
