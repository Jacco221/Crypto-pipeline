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

    if not md_path.exists():
        return send_message("Pipeline rapport niet gevonden.")

    md = md_path.read_text(encoding="utf-8")

    # Parse regime
    regime = "ONBEKEND"
    regime_line = ""
    advice_line = ""
    for line in md.splitlines():
        if line.startswith("> Market regime:"):
            regime_line = line[2:]  # strip "> "
            if "RISK_ON" in line:
                regime = "RISK_ON"
            elif "CAUTIOUS" in line:
                regime = "CAUTIOUS"
            elif "RISK_OFF" in line:
                regime = "RISK_OFF"
        if line.startswith("> Advies:"):
            advice_line = line[2:]

    # Parse top 3 uit CSV
    scores_path = reports_dir / "scores_latest.csv"
    top_lines = ""
    if scores_path.exists():
        import csv
        with open(scores_path) as f:
            reader = csv.DictReader(f)
            rows = list(reader)[:3]
        for i, row in enumerate(rows, 1):
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

    # Emoji per regime
    emoji = {"RISK_ON": "🟢", "CAUTIOUS": "🟡", "RISK_OFF": "🔴"}.get(regime, "⚪")

    msg = (
        f"{emoji} <b>Dagelijkse Crypto Update</b>\n\n"
        f"<b>Regime:</b> {regime_line}\n"
        f"<b>Advies:</b> {advice_line}\n\n"
        f"<b>Top 3:</b>\n{top_lines}\n"
        f"{alloc_text}"
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

    # Filter op minimum score
    alerts = [r for r in rows if float(r.get("dip_score", 0)) >= min_score]
    if not alerts:
        return False  # geen alert nodig

    lines = ""
    for r in alerts[:5]:
        sym = r.get("symbol", "?")
        score = r.get("dip_score", "?")
        chg_24h = r.get("chg_24h_%", "?")
        chg_7d = r.get("chg_7d_%", "?")
        lines += f"  <b>{sym}</b> — score {score} (24h: {chg_24h}%, 7d: {chg_7d}%)\n"

    msg = (
        f"🔔 <b>Dip Alert!</b>\n\n"
        f"{len(alerts)} coin(s) met sterke dip-kans:\n\n"
        f"{lines}\n"
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

    msg = (
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
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Telegram notificaties voor crypto pipeline")
    ap.add_argument("--test", action="store_true", help="Stuur testbericht")
    ap.add_argument("--daily", action="store_true", help="Stuur dagelijkse samenvatting")
    ap.add_argument("--dips", action="store_true", help="Stuur dip-alert (indien kansen)")
    ap.add_argument("--reports-dir", type=str, default="data/reports")
    ap.add_argument("--min-dip-score", type=float, default=0.7)
    args = ap.parse_args()

    reports = Path(args.reports_dir)

    if args.test:
        ok = send_message("🧪 <b>Test:</b> Crypto Pipeline notificaties werken!")
        print("OK" if ok else "FAILED")
    if args.daily:
        ok = send_daily_summary(reports)
        print(f"Daily: {'OK' if ok else 'FAILED'}")
    if args.dips:
        ok = send_dip_alert(reports, min_score=args.min_dip_score)
        print(f"Dips: {'OK — alert verstuurd' if ok else 'Geen dips boven drempel'}")


if __name__ == "__main__":
    main()
