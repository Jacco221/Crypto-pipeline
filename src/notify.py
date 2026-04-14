# src/notify.py
"""
Telegram notificatie-module voor de crypto pipeline.

Stuurt alerts naar Telegram bij:
- Dagelijkse samenvatting (regime + top coins)
- Dip-kansen (score >= drempel)
- Regime-wisselingen

Configuratie via environment variables:
    TELEGRAM_BOT_TOKEN  — bot token van @BotFather
    TELEGRAM_CHAT_ID    — je persoonlijke chat ID

Standalone test: python3 -m src.notify --test
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

import requests


BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


def send_message(text: str, parse_mode: str = "HTML") -> bool:
    """Stuur een bericht via Telegram Bot API."""
    if not BOT_TOKEN or not CHAT_ID:
        print("[Notify] TELEGRAM_BOT_TOKEN of TELEGRAM_CHAT_ID niet ingesteld", file=sys.stderr)
        return False

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": parse_mode,
    }

    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code == 200 and r.json().get("ok"):
            return True
        print(f"[Notify] Telegram fout: {r.text[:200]}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"[Notify] Telegram exceptie: {e}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# Dagelijkse samenvatting
# ---------------------------------------------------------------------------

def send_daily_summary(reports_dir: Path) -> bool:
    """Stuur dagelijkse samenvatting: regime + top 3 + allocatie."""
    md_path = reports_dir / "top5_latest.md"
    alloc_path = reports_dir / "allocation_latest.json"

    # Parse regime — uit md of uit regime_latest.json als fallback
    regime = "ONBEKEND"
    regime_line = ""
    advice_line = ""

    if md_path.exists():
        md = md_path.read_text(encoding="utf-8")
        for line in md.splitlines():
            if line.startswith("> Market regime:"):
                regime_line = line[2:]
                if "RISK_ON" in line:
                    regime = "RISK_ON"
                elif "CAUTIOUS" in line:
                    regime = "CAUTIOUS"
                elif "RISK_OFF" in line:
                    regime = "RISK_OFF"
            if line.startswith("> Advies:"):
                advice_line = line[2:]

    if regime == "ONBEKEND":
        regime_path = reports_dir / "regime_latest.json"
        if regime_path.exists():
            try:
                rd = json.loads(regime_path.read_text())
                regime = rd.get("regime", "ONBEKEND")
                regime_line = f"Market regime: {regime} (score {rd.get('regime_score','?')})"
                advice_map = {
                    "RISK_OFF": "STABLECOIN (risk-off)",
                    "CAUTIOUS": "HALVE ALLOCATIE (cautious)",
                    "RISK_ON": "Volg Top-picks (risk-on)",
                }
                advice_line = advice_map.get(regime, "")
            except Exception:
                pass

    # Parse top 5 koopbare coins uit CSV (pump-geblokkeerde coins overslaan)
    scores_path = reports_dir / "scores_latest.csv"
    top_lines = ""
    if scores_path.exists():
        import csv
        max_7d = 200.0 if regime == "RISK_ON" else 100.0
        with open(scores_path) as f:
            all_rows = list(csv.DictReader(f))
        buyable = []
        for row in all_rows:
            try:
                chg = float(row.get("chg_7d_raw", 0) or 0)
            except ValueError:
                chg = 0.0
            if chg < max_7d:
                buyable.append(row)
            if len(buyable) == 5:
                break
        for i, row in enumerate(buyable, 1):
            sym = row.get("symbol", "?")
            total = row.get("Total_%", "?")
            top_lines += f"  {i}. <b>{sym}</b> — {total}%\n"

    # Allocatie
    alloc_text = ""
    if alloc_path.exists():
        alloc = json.loads(alloc_path.read_text())
        decision = alloc.get("decision", "?")
        coins = alloc.get("allocation", {})
        parts = [f"{k} {v*100:.0f}%" for k, v in coins.items()]
        alloc_text = f"Allocatie: {decision} ({', '.join(parts)})"

    # Rotatie
    rotation_text = ""
    rotation_path = reports_dir / "rotation_latest.json"
    if rotation_path.exists():
        try:
            rot = json.loads(rotation_path.read_text())
            rot_label = rot.get("rotation", "NEUTRAL")
            rot_emoji = {"ALT_SEASON": "🌀", "BTC_SEASON": "₿", "NEUTRAL": "⚖️"}.get(rot_label, "⚖️")
            rotation_text = (
                f"\n{rot_emoji} <b>Rotatie:</b> {rot_label} "
                f"(BTC {rot.get('btc_7d', 0):+.1f}% vs alts {rot.get('alt_median_7d', 0):+.1f}%)"
            )
        except Exception:
            pass

    # Emoji per regime
    emoji = {"RISK_ON": "🟢", "CAUTIOUS": "🟡", "RISK_OFF": "🔴"}.get(regime, "⚪")

    msg = (
        f"{emoji} <b>Dagelijkse Crypto Update</b>\n\n"
        f"<b>Regime:</b> {regime_line}\n"
        f"<b>Advies:</b> {advice_line}"
        f"{rotation_text}\n\n"
        f"<b>Top 5 (koopbaar):</b>\n{top_lines}\n"
        f"{alloc_text}"
    )

    return send_message(msg)


# ---------------------------------------------------------------------------
# Scan update — kort bericht elke scan
# ---------------------------------------------------------------------------

def send_scan_update(reports_dir: Path) -> bool:
    """
    Stuur kort statusbericht na elke scan.
    Altijd verstuurd (ook bij HOLD/RISK_OFF), zodat je weet dat het systeem draait.
    """
    import datetime

    # Regime
    regime = "ONBEKEND"
    regime_path = reports_dir / "regime_latest.json"
    if regime_path.exists():
        try:
            rd = json.loads(regime_path.read_text())
            regime = rd.get("regime", "ONBEKEND")
        except Exception:
            pass

    regime_emoji = {"RISK_ON": "🟢", "CAUTIOUS": "🟡", "RISK_OFF": "🔴"}.get(regime, "⚪")

    # Portfolio — lees rechtstreeks van Kraken (niet van gitignored state file)
    portfolio_lines = ""
    total_portfolio_usd = 0.0
    action_text = "⏸ Geen actie"
    try:
        from src.kraken import get_balance, find_usd_pair, get_ticker
        from src.state import load_positions
        IGNORE = {"ZUSD", "USD", "ZEUR", "EUR", "USDC", "USDG", "USDT"}
        balances = get_balance()

        # USD saldo
        usd_bal = balances.get("ZUSD", balances.get("USD", 0))
        if usd_bal > 1:
            portfolio_lines += f"  💵 USD: ${usd_bal:.2f}\n"
            total_portfolio_usd += usd_bal

        # Crypto posities
        saved = {p["symbol"].upper(): p for p in load_positions()}
        for asset, amount in balances.items():
            if asset in IGNORE or amount <= 0:
                continue
            # Normaliseer symbol
            sym = asset
            if asset == "XXBT": sym = "BTC"
            elif asset == "XETH": sym = "ETH"
            pair = find_usd_pair(sym) or find_usd_pair(asset)
            if not pair:
                continue
            try:
                ticker = get_ticker(pair)
                price = ticker.get("last", 0)
                est_usd = amount * price
                if est_usd < 5:
                    continue
                total_portfolio_usd += est_usd
                # P&L vs entry
                entry = saved.get(sym.upper(), {}).get("entry_price", 0)
                pnl_str = f" ({(price-entry)/entry*100:+.1f}%)" if entry else ""
                portfolio_lines += f"  🪙 {sym}: {amount:.4f} (~${est_usd:.2f}){pnl_str}\n"
            except Exception:
                pass
    except Exception:
        portfolio_lines = "  (Kraken niet bereikbaar)\n"

    # Top coin uit scores
    top_text = ""
    scores_path = reports_dir / "scores_latest.csv"
    if scores_path.exists():
        try:
            import csv
            with open(scores_path) as f:
                row = next(csv.DictReader(f), None)
            if row:
                top_text = f"🏆 Top coin: <b>{row['symbol']}</b> ({row.get('Total_%','?')}%)\n"
        except Exception:
            pass

    # Actie bepalen op basis van regime
    if regime == "RISK_OFF":
        action_text = "⏸ Geen actie — wachten op herstel"
    elif regime == "CAUTIOUS":
        action_text = "⏸ Geen actie — al in positie of wachten"
    elif regime == "RISK_ON":
        action_text = "✅ Actief handelen"

    now = datetime.datetime.utcnow().strftime("%H:%M UTC")
    total_str = f"  💼 Totaal: ~${total_portfolio_usd:.2f}\n" if total_portfolio_usd > 0 else ""

    msg = (
        f"{regime_emoji} <b>{regime}</b> — {now}\n\n"
        f"<b>Portfolio:</b>\n{portfolio_lines}{total_str}\n"
        f"{top_text}"
        f"{action_text}"
    )
    return send_message(msg)


# ---------------------------------------------------------------------------
# Dip alert
# ---------------------------------------------------------------------------

def send_dip_alert(reports_dir: Path, min_score: float = 0.7) -> bool:
    """Stuur alert als er dip-kansen zijn met score >= min_score."""
    dips_path = reports_dir / "dips_latest.csv"
    if not dips_path.exists():
        return False

    import csv
    with open(dips_path) as f:
        rows = list(csv.DictReader(f))

    # Filter op minimum score én herstelmomentum (rs_recovery >= 0.3)
    # Zonder herstel is het geen dip maar een dalende trend
    alerts = [
        r for r in rows
        if float(r.get("dip_score", 0)) >= min_score
        and float(r.get("rs_recovery", 0)) >= 0.3
    ]
    if not alerts:
        return False  # geen alert nodig

    prio_emoji = {"A": "🟢", "B": "🟡", "C": "⚪"}

    lines = ""
    for r in alerts[:5]:
        sym = r.get("symbol", "?")
        score = r.get("dip_score", "?")
        chg_24h = r.get("chg_24h_%", "?")
        chg_7d = r.get("chg_7d_%", "?")
        prio = r.get("priority", "C")
        emoji = prio_emoji.get(prio, "⚪")
        lines += f"  {emoji} <b>{sym}</b> [{prio}] — score {score} (24h: {chg_24h}%, 7d: {chg_7d}%)\n"

    msg = (
        f"🔔 <b>Dip Alert!</b>\n\n"
        f"{len(alerts)} coin(s) met dip-kans:\n\n"
        f"{lines}\n"
        f"🟢 A = sterke kans | 🟡 B = matig | ⚪ C = watchlist\n"
        f"Check de details en overweeg instappen als regime het toelaat."
    )

    return send_message(msg)


# ---------------------------------------------------------------------------
# Regime-wissel alert
# ---------------------------------------------------------------------------

def send_regime_change(old_regime: str, new_regime: str,
                       reports_dir: Path) -> bool:
    """Stuur alert bij regime-wissel."""
    emoji_map = {"RISK_ON": "🟢", "CAUTIOUS": "🟡", "RISK_OFF": "🔴"}
    old_e = emoji_map.get(old_regime, "⚪")
    new_e = emoji_map.get(new_regime, "⚪")

    action = ""
    if new_regime == "RISK_ON":
        action = "Instappen toegestaan — volg allocatie-advies"
    elif new_regime == "CAUTIOUS":
        action = "Halve allocatie — voorzichtig instappen"
    elif new_regime == "RISK_OFF":
        action = "Alles naar stablecoin (USDC)"

    msg = (
        f"⚡ <b>REGIME WISSEL!</b>\n\n"
        f"{old_e} {old_regime} → {new_e} {new_regime}\n\n"
        f"<b>Actie:</b> {action}"
    )

    return send_message(msg)


# ---------------------------------------------------------------------------
# Trade voorstel via Telegram
# ---------------------------------------------------------------------------

def send_trade_proposal(plan: dict) -> bool:
    """Stuur trade-voorstel naar Telegram voor bevestiging."""
    if "error" in plan:
        return send_message(f"❌ Trade fout: {plan['error']}")

    sell = plan["step1_sell"]
    buy = plan["step2_buy"]

    regime_header = plan.get("regime_header", "")
    if regime_header:
        regime_header += "\n\n"

    msg = (
        f"{regime_header}"
        f"💱 <b>Trade Voorstel</b>\n\n"
        f"<b>Stap 1 — Verkoop:</b>\n"
        f"  {sell['volume']:.4f} {plan['current']} → ${sell['net_usd']:.2f}\n"
        f"  Prijs: ${sell['price']:.4f} | Fee: ${sell['fee_usd']:.2f}\n\n"
        f"<b>Stap 2 — Koop:</b>\n"
        f"  ~{buy['est_coins']:.4f} {plan['target']} met ${buy['usd_amount']:.2f}\n"
        f"  Prijs: ${buy['price']:.4f} | Fee: ${buy['fee_usd']:.2f}\n\n"
        f"<b>Totale fee:</b> ${plan['total_fee_usd']:.2f}\n\n"
        f"Stuur <b>JA</b> om uit te voeren of <b>NEE</b> om te annuleren."
    )

    return send_message(msg)


def send_trade_result(result: dict) -> bool:
    """Stuur trade-resultaat naar Telegram."""
    if "error" in result:
        return send_message(f"❌ Trade mislukt: {result['error']}")

    msg = (
        f"✅ <b>Trade Uitgevoerd!</b>\n\n"
        f"Verkocht: {result['sold']}\n"
        f"Gekocht: {result['bought']}\n\n"
        f"Sell TX: {', '.join(result.get('sell_txids', []))}\n"
        f"Buy TX: {', '.join(result.get('buy_txids', []))}"
    )

    return send_message(msg)


def send_balance(balance_text: str) -> bool:
    """Stuur account balans naar Telegram."""
    msg = f"💰 <b>Kraken Balans</b>\n\n<pre>{balance_text}</pre>"
    return send_message(msg)


def check_confirmation(timeout_seconds: int = 300) -> Optional[str]:
    """
    Wacht op bevestiging via Telegram (JA/NEE).
    Pollt de bot voor nieuwe berichten.
    Timeout na 5 minuten.
    """
    if not BOT_TOKEN:
        return None

    start = time.time()
    last_update_id = 0

    # Haal eerst bestaande updates op om ze te skippen
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
            params={"timeout": 1}, timeout=10
        )
        data = r.json()
        if data.get("result"):
            last_update_id = data["result"][-1]["update_id"]
    except Exception:
        pass

    while time.time() - start < timeout_seconds:
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
                params={"offset": last_update_id + 1, "timeout": 10},
                timeout=15
            )
            data = r.json()
            for update in data.get("result", []):
                last_update_id = update["update_id"]
                msg = update.get("message", {})
                text = (msg.get("text") or "").strip().upper()
                chat_id = str(msg.get("chat", {}).get("id", ""))

                if chat_id == CHAT_ID and text in ("JA", "NEE", "YES", "NO"):
                    return text

        except Exception:
            time.sleep(5)

    return None  # timeout


# ---------------------------------------------------------------------------
# Status rapport
# ---------------------------------------------------------------------------

def send_status_report(reports_dir: Path) -> bool:
    """
    Stuur volledig status-overzicht op aanvraag.

    Bevat:
    - Huidig regime + signalen breakdown
    - Huidige positie + P&L (indien beschikbaar)
    - Top 3 coins
    - Actieve dip-kansen
    """
    lines = []

    # 1. Regime
    regime = "ONBEKEND"
    regime_score = "?"
    signal_lines = ""

    regime_path = reports_dir / "regime_latest.json"
    md_path = reports_dir / "top5_latest.md"

    if regime_path.exists():
        try:
            rd = json.loads(regime_path.read_text())
            regime = rd.get("regime", "ONBEKEND")
            regime_score = rd.get("regime_score", "?")
            s = rd.get("signals", {})

            def tick(v): return "✅" if v else "❌"

            signal_lines = (
                f"├ BTC vs MA20: {tick(s.get('btc_above_ma20'))} "
                f"({rd.get('last_close', '?')} / {rd.get('ma20', '?')})\n"
                f"├ MA20 vs MA200: {tick(s.get('ma20_above_ma200'))}\n"
                f"├ Fear &amp; Greed: {s.get('fear_greed_value', '?')} {tick(s.get('fg_not_extreme_fear'))}\n"
                f"├ DXY dalend: {tick(s.get('dxy_bullish'))}\n"
                f"├ Funding rate: {s.get('funding_rate_pct', '?')}% "
                f"{tick(s.get('funding_signal') == 'OVERSOLD')}\n"
                f"└ MVRV: {s.get('mvrv', '?')} "
                f"{tick(s.get('mvrv_buy_zone'))}"
            )
        except Exception:
            pass
    elif md_path.exists():
        for line in md_path.read_text().splitlines():
            if "RISK_ON" in line:
                regime = "RISK_ON"
            elif "CAUTIOUS" in line:
                regime = "CAUTIOUS"
            elif "RISK_OFF" in line:
                regime = "RISK_OFF"

    regime_emoji = {"RISK_ON": "🟢", "CAUTIOUS": "🟡", "RISK_OFF": "🔴"}.get(regime, "⚪")
    lines.append(f"📊 <b>Status Overzicht</b>\n")
    lines.append(f"{regime_emoji} <b>Regime: {regime}</b> (score {regime_score})")
    if signal_lines:
        lines.append(signal_lines)

    # 2. Posities + P&L (ondersteunt meerdere coins)
    lines.append("")
    try:
        from src.state import load_positions
        positions = load_positions()
        if positions:
            try:
                from src.kraken import find_usd_pair, get_ticker
                kraken_available = True
            except Exception:
                kraken_available = False

            pos_lines = []
            for pos in positions:
                sym = pos["symbol"]
                entry = pos.get("entry_price", 0)
                peak = pos.get("peak_price", entry)
                entry_usd = pos.get("entry_usd", 0)
                pnl_text = ""
                if kraken_available and entry:
                    try:
                        pair = find_usd_pair(sym)
                        if pair:
                            ticker = get_ticker(pair)
                            current = ticker.get("last", 0)
                            if current:
                                pnl_pct = (current - entry) / entry * 100
                                pnl_text = f" <b>{pnl_pct:+.1f}%</b>"
                    except Exception:
                        pass
                pos_lines.append(
                    f"  <b>{sym}</b>{pnl_text} | instap ${entry:.4f} "
                    f"(${entry_usd:.0f}) | piek ${peak:.4f}"
                )

            label = "Portefeuille" if len(positions) > 1 else "Positie"
            lines.append(f"💼 <b>{label}:</b>\n" + "\n".join(pos_lines))
        else:
            lines.append("💼 <b>Positie: USD</b> (geen coin)")
    except Exception:
        lines.append("💼 Positie: onbekend")

    # 3. Top 5 koopbare coins (pump-geblokkeerde coins overslaan)
    lines.append("")
    scores_path = reports_dir / "scores_latest.csv"
    if scores_path.exists():
        try:
            import csv
            max_7d = 200.0 if regime == "RISK_ON" else 100.0
            with open(scores_path) as f:
                all_rows = list(csv.DictReader(f))
            buyable = []
            for row in all_rows:
                try:
                    chg = float(row.get("chg_7d_raw", 0) or 0)
                except ValueError:
                    chg = 0.0
                if chg < max_7d:
                    buyable.append(row)
                if len(buyable) == 5:
                    break
            top_text = "\n".join(
                f"  {i+1}. <b>{r['symbol']}</b> — {r.get('Total_%', '?')}%"
                for i, r in enumerate(buyable)
            )
            lines.append(f"📈 <b>Top 5 (koopbaar):</b>\n{top_text}")
        except Exception:
            pass

    # 4. Dip-kansen
    dips_path = reports_dir / "dips_latest.csv"
    if dips_path.exists():
        try:
            import csv
            with open(dips_path) as f:
                dips = [r for r in csv.DictReader(f)
                        if float(r.get("dip_score", 0)) >= 0.6][:3]
            if dips:
                prio_emoji = {"A": "🟢", "B": "🟡", "C": "⚪"}
                dip_text = "\n".join(
                    f"  {prio_emoji.get(r.get('priority','C'), '⚪')} "
                    f"<b>{r['symbol']}</b> [{r.get('priority','?')}] "
                    f"score {float(r['dip_score']):.2f}"
                    for r in dips
                )
                lines.append(f"\n🔔 <b>Dip-kansen:</b>\n{dip_text}")
        except Exception:
            pass

    if regime in ("RISK_OFF", "UNKNOWN"):
        lines.append("\n⏳ Wachten op RISK_ON — geen aankopen.")

    return send_message("\n".join(lines))


def send_performance_report() -> bool:
    """
    Stuur performance rapport op basis van trade log.

    Toont:
    - Totaal aantal trades + gesloten posities
    - Win rate
    - Totale P&L in USD + gemiddeld per trade
    - Beste en slechtste trade
    - Stop-losses en take-profits
    - Laatste 5 trades
    """
    try:
        from src.trade_log import get_performance_summary
        perf = get_performance_summary()
    except Exception as e:
        return send_message(f"❌ Performance data niet beschikbaar: {e}")

    if perf["total_trades"] == 0:
        return send_message(
            "📊 <b>Performance Rapport</b>\n\n"
            "Nog geen trades geregistreerd.\n"
            "Trades worden automatisch bijgehouden zodra het systeem handelt."
        )

    # Kleuring op basis van resultaat
    pnl_total = perf["total_pnl_usd"]
    pnl_emoji = "📈" if pnl_total >= 0 else "📉"
    win_emoji = "🟢" if perf["win_rate_pct"] >= 50 else "🔴"

    best = f"+{perf['best_trade_pct']:.1f}%" if perf["best_trade_pct"] is not None else "n.v.t."
    worst = f"{perf['worst_trade_pct']:.1f}%" if perf["worst_trade_pct"] is not None else "n.v.t."
    avg = f"{perf['avg_pnl_pct']:+.1f}%" if perf["closed_trades"] > 0 else "n.v.t."

    lines = [
        f"📊 <b>Performance Rapport</b>\n",
        f"Trades: {perf['total_trades']} totaal | {perf['closed_trades']} gesloten",
        f"{win_emoji} Win rate: <b>{perf['win_rate_pct']:.0f}%</b>",
        f"{pnl_emoji} Totale P&L: <b>${pnl_total:+.2f}</b>",
        f"Gem. per trade: <b>{avg}</b>",
        f"Beste: <b>{best}</b> | Slechtste: <b>{worst}</b>",
        f"Stop-losses: {perf['stop_losses']} | Take-profits: {perf['take_profits']}",
    ]

    # Laatste 5 trades
    log = perf.get("log", [])
    if log:
        lines.append("\n<b>Laatste trades:</b>")
        for r in reversed(log[-5:]):
            action = r.get("action", "?")
            sym = r.get("symbol", "?")
            dt = r.get("datetime", "")[:10]
            pnl = r.get("pnl_pct")
            pnl_str = f" ({pnl:+.1f}%)" if pnl is not None else ""
            a_emoji = {"BUY": "🟢", "SELL": "⚪", "STOP_LOSS": "🛑", "TAKE_PROFIT": "🎯"}.get(action, "•")
            lines.append(f"  {a_emoji} {dt} {action} <b>{sym}</b>{pnl_str}")

    return send_message("\n".join(lines))


def handle_telegram_commands(reports_dir: Path) -> int:
    """
    Check voor nieuwe Telegram-commando's en beantwoord ze.
    Verwerkt alle ongelezen berichten van de afgelopen periode.

    Ondersteunde commando's:
        status / /status    → stuur status overzicht
        rapport / /rapport  → stuur performance rapport

    Retourneert aantal verwerkte commando's.
    """
    if not BOT_TOKEN or not CHAT_ID:
        return 0

    handled = 0
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
            params={"timeout": 2, "allowed_updates": ["message"]},
            timeout=10,
        )
        updates = r.json().get("result", [])
        if not updates:
            return 0

        last_id = updates[-1]["update_id"]

        for update in updates:
            msg = update.get("message", {})
            text = (msg.get("text") or "").strip().lower()
            chat_id = str(msg.get("chat", {}).get("id", ""))

            if chat_id != CHAT_ID:
                continue

            if text in ("status", "/status"):
                print(f"[Notify] Status-commando ontvangen — rapport sturen...")
                send_status_report(reports_dir)
                handled += 1
            elif text in ("rapport", "/rapport"):
                print(f"[Notify] Rapport-commando ontvangen — performance sturen...")
                send_performance_report()
                handled += 1

        # Markeer alle updates als gelezen
        requests.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
            params={"offset": last_id + 1, "timeout": 1},
            timeout=5,
        )
    except Exception as e:
        print(f"[Notify] Command handler fout: {e}")

    return handled


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Telegram notificaties voor crypto pipeline")
    ap.add_argument("--test", action="store_true", help="Stuur testbericht")
    ap.add_argument("--scan-update", action="store_true", help="Stuur kort scan-update bericht")
    ap.add_argument("--daily", action="store_true", help="Stuur dagelijkse samenvatting")
    ap.add_argument("--dips", action="store_true", help="Stuur dip-alert (indien kansen)")
    ap.add_argument("--status", action="store_true", help="Stuur status overzicht nu")
    ap.add_argument("--rapport", action="store_true", help="Stuur performance rapport nu")
    ap.add_argument("--commands", action="store_true", help="Verwerk Telegram commando's")
    ap.add_argument("--reports-dir", type=str, default="data/reports")
    ap.add_argument("--min-dip-score", type=float, default=0.7)
    args = ap.parse_args()

    reports = Path(args.reports_dir)

    if args.test:
        ok = send_message("🧪 <b>Test:</b> Crypto Pipeline notificaties werken!")
        print("OK" if ok else "FAILED")
    if args.scan_update:
        ok = send_scan_update(reports)
        print(f"Scan update: {'OK' if ok else 'FAILED'}")
    if args.daily:
        ok = send_daily_summary(reports)
        print(f"Daily: {'OK' if ok else 'FAILED'}")
    if args.dips:
        ok = send_dip_alert(reports, min_score=args.min_dip_score)
        print(f"Dips: {'OK — alert verstuurd' if ok else 'Geen dips boven drempel'}")
    if args.status:
        ok = send_status_report(reports)
        print(f"Status: {'OK' if ok else 'FAILED'}")
    if args.rapport:
        ok = send_performance_report()
        print(f"Rapport: {'OK' if ok else 'FAILED'}")
    if args.commands:
        n = handle_telegram_commands(reports)
        print(f"Commands: {n} verwerkt")


if __name__ == "__main__":
    main()
