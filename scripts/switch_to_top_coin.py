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

TARGET = "XPL"

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

if not current_sym:
    send_message("❌ Geen positie gevonden om te switchen.")
    sys.exit(1)

# Al in de target coin — geen switch nodig
if current_sym.upper() == TARGET.upper():
    send_message(f"✅ Al in {TARGET} ({current_amount:.0f} coins ~${est_usd:.2f}) — geen switch nodig.")
    print(f"Al in {TARGET}, geen actie.")
    sys.exit(0)

# Bepaal max_usd op basis van regime (CAUTIOUS = 50%)
from src.market_regime import determine_market_regime
regime_data = determine_market_regime()
regime = regime_data["regime"]

usd_balance = balances.get("ZUSD", balances.get("USD", 0))
ticker_current = get_ticker(find_usd_pair(current_sym) or "")
current_usd = current_amount * ticker_current.get("last", 0)
total_portfolio = usd_balance + current_usd
max_usd = total_portfolio * 0.5 if regime == "CAUTIOUS" else None
max_invest = max_usd if max_usd else total_portfolio
print(f"Regime: {regime} | Portfolio: ${total_portfolio:.2f} | Max invest: ${max_invest:.2f}")

# Annuleer eerst alle open orders (bijv. stop-loss die coins blokkeert)
print(f"Annuleer open orders voor {current_sym}...")
try:
    from src.kraken import cancel_all_orders
    cancel_result = cancel_all_orders()
    print(f"Open orders geannuleerd: {cancel_result}")
    import time; time.sleep(2)  # Wacht tot annulering verwerkt is
except Exception as e:
    print(f"Waarschuwing: orders annuleren mislukt: {e}")

# Voer switch uit
print(f"Switch {current_sym} → {TARGET}...")
result = execute_switch(current_sym, TARGET, max_usd=max_usd)

# Verificeer
verif = verify_position(TARGET)
if verif["confirmed"]:
    entry_price = verif["price"]
    actual_usd = verif["est_usd"]
    save_positions([{
        "symbol": TARGET,
        "entry_price": entry_price,
        "entry_usd": actual_usd,
        "peak_price": entry_price,
        "source": "manual_correction",
    }])

    # Stop-loss plaatsen
    sl_price = round(entry_price * 0.85, 6)
    try:
        from src.kraken import place_stop_loss_order
        actual_amount = verif["amount"]
        place_stop_loss_order(find_usd_pair(TARGET), actual_amount, sl_price)
        sl_note = f"\n🛑 Stop-loss: ${sl_price:.4f} (-15%)"
    except Exception as e:
        sl_note = f"\n⚠️ Stop-loss mislukt: {e}"

    send_message(
        f"✅ <b>Terug in Plasma (XPL)</b>\n\n"
        f"Verkocht: KTA\n"
        f"Gekocht: <b>XPL</b> @ ${entry_price:.4f}\n"
        f"Bedrag: ~${actual_usd:.2f}"
        f"{sl_note}"
    )
    print("✅ Switch naar XPL geslaagd.")
else:
    send_message(f"❌ Switch naar XPL mislukt — verificatie gefaald.")
    print("❌ Verificatie mislukt.")
