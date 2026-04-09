#!/usr/bin/env python3
"""
Eenmalig script: switch terug naar de #1 pipeline coin (XPL).
"""
import sys
import os
import json
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.kraken import get_balance, find_usd_pair, get_ticker, execute_switch, verify_position
from src.notify import send_message
from src.state import save_positions, clear_positions

TARGET = os.environ.get("TARGET_COIN", "XPL")

# Huidige positie bepalen
balances = get_balance()
IGNORE = {"ZUSD", "USD", "ZEUR", "EUR", "USDC", "USDG", "USDT"}

current_sym = None
current_amount = 0
for asset, amount in balances.items():
    if asset in IGNORE or amount <= 0:
        continue
    pair = find_usd_pair(asset)
    if not pair:
        # Probeer zonder leading X
        pair = find_usd_pair(asset[1:]) if len(asset) > 3 else None
    if pair:
        ticker = get_ticker(pair)
        est_usd = amount * ticker.get("last", 0)
        if est_usd > 5:
            current_sym = asset
            current_amount = amount
            print(f"Huidige positie: {asset} ({amount:.4f} ~${est_usd:.2f})")
            break

# Regime bepalen (nodig voor 50% CAUTIOUS regel)
from src.market_regime import determine_market_regime
regime_data = determine_market_regime()
regime = regime_data["regime"]
usd_balance = balances.get("ZUSD", balances.get("USD", 0))

# ── GEVAL 1: Al in target coin ──────────────────────────────────────────────
if current_sym and current_sym.upper() == TARGET.upper():
    send_message(f"✅ Al in {TARGET} ({current_amount:.0f} coins ~${est_usd:.2f}) — geen switch nodig.")
    print(f"Al in {TARGET}, geen actie.")
    sys.exit(0)

# ── GEVAL 2: In een andere coin → switch ────────────────────────────────────
if current_sym:
    ticker_current = get_ticker(find_usd_pair(current_sym) or "")
    current_usd = current_amount * ticker_current.get("last", 0)
    total_portfolio = usd_balance + current_usd
    max_usd = total_portfolio * 0.5 if regime == "CAUTIOUS" else None
    max_invest = max_usd if max_usd else total_portfolio
    print(f"Regime: {regime} | Portfolio: ${total_portfolio:.2f} | Max invest: ${max_invest:.2f}")

    # Annuleer open orders (stop-loss blokkeert de coins)
    print(f"Annuleer open orders voor {current_sym}...")
    try:
        from src.kraken import cancel_all_orders
        cancel_result = cancel_all_orders()
        print(f"Open orders geannuleerd: {cancel_result}")
        import time; time.sleep(2)
    except Exception as e:
        print(f"Waarschuwing: orders annuleren mislukt: {e}")

    print(f"Switch {current_sym} → {TARGET}...")
    result = execute_switch(current_sym, TARGET, max_usd=max_usd)

# ── GEVAL 3: Alleen USD → direct kopen ──────────────────────────────────────
else:
    if usd_balance < 5:
        send_message(f"❌ Geen positie en geen USD om {TARGET} te kopen.")
        sys.exit(1)
    total_portfolio = usd_balance
    invest_usd = usd_balance * 0.5 if regime == "CAUTIOUS" else usd_balance
    print(f"Geen crypto positie — koop {TARGET} vanuit USD")
    print(f"Regime: {regime} | USD: ${usd_balance:.2f} | Inzet: ${invest_usd:.2f}")

    pair = find_usd_pair(TARGET)
    if not pair:
        send_message(f"❌ Geen USD pair gevonden voor {TARGET}")
        sys.exit(1)

    from src.kraken import place_market_order
    import time
    send_message(
        f"🟢 <b>Aankoop gestart: {TARGET}</b>\n\n"
        f"💵 USD beschikbaar: ${usd_balance:.2f}\n"
        f"📋 Inzet: ${invest_usd:.2f} ({'CAUTIOUS 50%' if regime == 'CAUTIOUS' else 'RISK_ON 100%'})"
    )
    place_market_order(pair, "buy", invest_usd)
    time.sleep(3)
    result = {"status": "COMPLETED"}  # Verificatie volgt hieronder

# ── VERIFICATIE & STOP-LOSS (beide gevallen) ────────────────────────────────
verif = verify_position(TARGET)
if verif["confirmed"]:
    entry_price = verif["price"]
    actual_usd = verif["est_usd"]
    actual_amount = verif["amount"]
    save_positions([{
        "symbol": TARGET,
        "entry_price": entry_price,
        "entry_usd": actual_usd,
        "peak_price": entry_price,
        "source": "manual_correction",
    }])

    TRAIL_PCT = 0.20  # 20% native trailing stop
    try:
        from src.kraken import place_native_trailing_stop
        place_native_trailing_stop(find_usd_pair(TARGET), actual_amount, trail_pct=TRAIL_PCT)
        sl_note = f"\n🛑 Native trailing stop: -{TRAIL_PCT*100:.0f}% (Kraken real-time)"
    except Exception as e:
        sl_note = f"\n⚠️ Trailing stop mislukt: {e}"

    from_str = f"Verkocht: <b>{current_sym}</b>\n" if current_sym else ""
    send_message(
        f"✅ <b>Gekocht: {TARGET}</b>\n\n"
        f"{from_str}"
        f"Gekocht: <b>{TARGET}</b> @ ${entry_price:.4f}\n"
        f"Bedrag: ~${actual_usd:.2f}"
        f"{sl_note}"
    )
    print(f"✅ {TARGET} gekocht en geverifieerd.")
else:
    send_message(f"❌ Aankoop {TARGET} mislukt — verificatie gefaald.")
    print("❌ Verificatie mislukt.")
    sys.exit(1)
