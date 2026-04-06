#!/usr/bin/env python3
"""
Corrigeert de allocatie naar 50/50 bij CAUTIOUS regime.
Verkoopt excess crypto terug naar USD.
"""
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.kraken import get_balance, find_usd_pair, get_ticker, place_market_order
from src.notify import send_message
from src.market_regime import determine_market_regime

# Check regime
regime_data = determine_market_regime()
regime = regime_data["regime"]
print(f"Regime: {regime}")

if regime == "RISK_ON":
    send_message("✅ Regime is RISK_ON — geen correctie nodig, 100% crypto is correct.")
    sys.exit(0)

# Haal balans op
balances = get_balance()
usd_balance = balances.get("ZUSD", balances.get("USD", 0))

# Zoek crypto posities
IGNORE = {"ZUSD", "USD", "ZEUR", "EUR", "USDC", "USDG", "USDT"}
crypto_positions = {}
for asset, amount in balances.items():
    if asset in IGNORE or amount <= 0:
        continue
    symbol = asset.replace("X", "").replace("Z", "")
    if asset == "XXBT":
        symbol = "BTC"
    elif asset == "XETH":
        symbol = "ETH"
    pair = find_usd_pair(symbol)
    if not pair:
        continue
    ticker = get_ticker(pair)
    est_usd = amount * ticker.get("last", 0)
    if est_usd > 5:
        crypto_positions[symbol] = {"amount": amount, "est_usd": est_usd, "pair": pair}

total_usd = usd_balance + sum(p["est_usd"] for p in crypto_positions.values())
target_crypto_usd = total_usd * 0.5
current_crypto_usd = sum(p["est_usd"] for p in crypto_positions.values())
excess_usd = current_crypto_usd - target_crypto_usd

print(f"Totaal portfolio: ${total_usd:.2f}")
print(f"Crypto: ${current_crypto_usd:.2f} | USD: ${usd_balance:.2f} | Target crypto: ${target_crypto_usd:.2f}")
print(f"Te verkopen: ${excess_usd:.2f}")

if excess_usd < 10:
    send_message(
        f"✅ Allocatie is al correct\n\n"
        f"Crypto: ${current_crypto_usd:.2f} | USD: ${usd_balance:.2f}"
    )
    sys.exit(0)

# Verkoop excess uit grootste crypto positie
largest_sym = max(crypto_positions, key=lambda s: crypto_positions[s]["est_usd"])
pos = crypto_positions[largest_sym]
sell_fraction = excess_usd / pos["est_usd"]
sell_amount = pos["amount"] * sell_fraction

print(f"Verkoop {sell_amount:.4f} {largest_sym} (~${excess_usd:.2f}) naar USD")

result = place_market_order(pos["pair"], "sell", sell_amount)

send_message(
    f"🔧 <b>Allocatie gecorrigeerd</b>\n\n"
    f"Regime: {regime} — max 50% in crypto\n\n"
    f"Verkocht: {sell_amount:.4f} <b>{largest_sym}</b> (~${excess_usd:.2f})\n"
    f"Nieuw: ~${target_crypto_usd:.2f} crypto | ~${target_crypto_usd:.2f} USD\n"
    f"TX: {', '.join(result.get('txid', []))}"
)
print("✅ Correctie uitgevoerd.")
