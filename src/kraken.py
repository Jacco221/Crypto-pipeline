# src/kraken.py
"""
Kraken trading module — handelt via het spot orderbook (Pro fees).

Flow (informeer + bevestig):
1. Pipeline bepaalt trade (SWITCH naar andere coin)
2. Module stuurt voorstel naar Telegram
3. Gebruiker bevestigt met 'ja'
4. Module voert uit: verkoop huidige coin → USD → koop nieuwe coin
5. Bevestiging naar Telegram

Configuratie via environment variables:
    KRAKEN_API_KEY     — API key
    KRAKEN_API_SECRET  — Private key (base64)

Alle trades gaan via USD pairs op het spot orderbook (0.26% taker fee).
Nooit via Kraken Convert (1-2% spread).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import sys
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

import requests


API_KEY = os.environ.get("KRAKEN_API_KEY", "")
API_SECRET = os.environ.get("KRAKEN_API_SECRET", "")
API_URL = "https://api.kraken.com"


# ---------------------------------------------------------------------------
# Kraken API authenticatie (HMAC-SHA512)
# ---------------------------------------------------------------------------

def _sign(urlpath: str, data: dict) -> dict:
    """Genereer Kraken API signature headers."""
    if not API_KEY or not API_SECRET:
        raise RuntimeError("KRAKEN_API_KEY of KRAKEN_API_SECRET niet ingesteld")

    postdata = urllib.parse.urlencode(data)
    encoded = (str(data["nonce"]) + postdata).encode()
    message = urlpath.encode() + hashlib.sha256(encoded).digest()

    mac = hmac.new(base64.b64decode(API_SECRET), message, hashlib.sha512)
    sigdigest = base64.b64encode(mac.digest()).decode()

    return {
        "API-Key": API_KEY,
        "API-Sign": sigdigest,
    }


def _private_request(endpoint: str, data: Optional[dict] = None) -> dict:
    """Doe een authenticated Kraken API call."""
    if data is None:
        data = {}
    data["nonce"] = str(int(time.time() * 1000))

    urlpath = f"/0/private/{endpoint}"
    headers = _sign(urlpath, data)

    r = requests.post(API_URL + urlpath, headers=headers, data=data, timeout=30)
    result = r.json()

    if result.get("error"):
        errors = result["error"]
        raise RuntimeError(f"Kraken API fout: {errors}")

    return result.get("result", {})


def _public_request(endpoint: str, params: Optional[dict] = None) -> dict:
    """Doe een publieke Kraken API call."""
    r = requests.get(f"{API_URL}/0/public/{endpoint}", params=params, timeout=30)
    result = r.json()
    if result.get("error"):
        raise RuntimeError(f"Kraken API fout: {result['error']}")
    return result.get("result", {})


# ---------------------------------------------------------------------------
# Account info
# ---------------------------------------------------------------------------

def get_balance() -> Dict[str, float]:
    """Haal account balans op. Retourneert {asset: amount}."""
    raw = _private_request("Balance")
    balances = {}
    for asset, amount in raw.items():
        amt = float(amount)
        if amt > 0.0:
            balances[asset] = amt
    return balances


def get_balance_summary() -> str:
    """Leesbare balans-samenvatting."""
    balances = get_balance()
    if not balances:
        return "Geen saldo gevonden."

    lines = []
    for asset, amount in sorted(balances.items()):
        lines.append(f"  {asset}: {amount:.6f}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tradeable symbols (cached)
# ---------------------------------------------------------------------------

_tradeable_cache: Optional[set] = None


def get_tradeable_symbols() -> set:
    """
    Haal alle coins op die op Kraken verhandelbaar zijn (USD pair).
    Cached na eerste aanroep.
    """
    global _tradeable_cache
    if _tradeable_cache is not None:
        return _tradeable_cache

    pairs = _public_request("AssetPairs")
    symbols = set()
    for pair_name, info in pairs.items():
        wsname = info.get("wsname", "")
        if "/USD" in wsname:
            base = wsname.split("/")[0].upper()
            symbols.add(base)
            # Kraken gebruikt XBT voor Bitcoin — voeg BTC toe als alias
            if base == "XBT":
                symbols.add("BTC")

    _tradeable_cache = symbols
    return symbols


# ---------------------------------------------------------------------------
# Pair lookup
# ---------------------------------------------------------------------------

def find_usd_pair(symbol: str) -> Optional[str]:
    """Vind het USD trading pair voor een coin op Kraken."""
    symbol = symbol.upper()
    pairs = _public_request("AssetPairs")

    # Directe match pogingen
    candidates = [
        f"{symbol}USD",
        f"{symbol}ZUSD",
        f"X{symbol}ZUSD",
        f"X{symbol}USD",
    ]

    for pair_name, info in pairs.items():
        wsname = info.get("wsname", "")
        base = info.get("base", "").upper()
        alt = info.get("altname", "").upper()

        # Check via wsname (meest betrouwbaar)
        if wsname == f"{symbol}/USD":
            return pair_name

        # Check directe naam
        if pair_name in candidates:
            return pair_name

        # Check altname
        if alt == f"{symbol}USD":
            return pair_name

    return None


# ---------------------------------------------------------------------------
# Trading
# ---------------------------------------------------------------------------

def get_ticker(pair: str) -> Dict[str, Any]:
    """Haal huidige prijs op voor een pair."""
    result = _public_request("Ticker", {"pair": pair})
    if result:
        key = list(result.keys())[0]
        data = result[key]
        return {
            "ask": float(data["a"][0]),
            "bid": float(data["b"][0]),
            "last": float(data["c"][0]),
            "volume_24h": float(data["v"][1]),
        }
    return {}


def place_market_order(pair: str, side: str, volume: float) -> Dict[str, Any]:
    """
    Plaats een market order (bestens).

    pair: Kraken pair naam (bijv. 'RIVERUSD')
    side: 'buy' of 'sell'
    volume: aantal coins (voor sell) of USD bedrag (voor buy met oflags=viqc)
    """
    data = {
        "pair": pair,
        "type": side,
        "ordertype": "market",
        "volume": str(volume),
    }

    # Bij kopen: gebruik 'viqc' flag om in quote currency (USD) te specificeren
    if side == "buy":
        data["oflags"] = "viqc"  # volume in quote currency

    result = _private_request("AddOrder", data)
    return result


def verify_position(symbol: str, min_usd: float = 5.0, retries: int = 3) -> dict:
    """
    Verifieer na een trade of de positie werkelijk op Kraken staat.
    Geeft terug: {confirmed, symbol, amount, est_usd, price}

    Retries: wacht tot 3x 3 seconden zodat een net geplaatst order tijd krijgt om te vullen.
    """
    KNOWN = {
        "XXBT": "BTC", "XETH": "ETH", "XXDG": "DOGE",
        "XXRP": "XRP", "XLTC": "LTC", "XXLM": "XLM",
        "XZEC": "ZEC", "XXMR": "XMR",
    }

    def _normalize(asset: str) -> str:
        """Zet Kraken asset naam om naar coin symbol (veilig — geen blinde X-strip)."""
        if asset in KNOWN:
            return KNOWN[asset]
        # Probeer origineel eerst (bijv. XPL, BANANAS31)
        if find_usd_pair(asset):
            return asset
        # Alleen strip leading X als het resultaat ook een geldig pair heeft
        if len(asset) > 3 and asset.startswith("X"):
            stripped = asset[1:]
            if find_usd_pair(stripped):
                return stripped
        return asset

    for attempt in range(retries):
        try:
            balances = get_balance()
            for asset, amount in balances.items():
                if amount <= 0:
                    continue

                normalized = _normalize(asset)
                if normalized.upper() == symbol.upper() or asset.upper() == symbol.upper():
                    pair = find_usd_pair(symbol)
                    price = 0.0
                    est_usd = 0.0
                    if pair:
                        ticker = get_ticker(pair)
                        price = ticker.get("last", 0)
                        est_usd = amount * price
                    if est_usd >= min_usd:
                        return {
                            "confirmed": True,
                            "symbol": symbol,
                            "amount": amount,
                            "est_usd": round(est_usd, 2),
                            "price": price,
                        }
        except Exception as e:
            return {"confirmed": False, "error": str(e)}

        if attempt < retries - 1:
            print(f"[Kraken] verify_position: {symbol} nog niet gevonden, wacht 4s... (poging {attempt+1}/{retries})")
            time.sleep(4)

    return {"confirmed": False, "symbol": symbol, "amount": 0, "est_usd": 0}


def place_stop_loss_order(pair: str, volume: float, stop_price: float) -> Dict[str, Any]:
    """
    Plaats een stop-loss order op Kraken (noodrem, -20%).
    Dit is een conditionele order die automatisch verkoopt als de prijs zakt.
    """
    data = {
        "pair": pair,
        "type": "sell",
        "ordertype": "stop-loss",
        "price": str(stop_price),
        "volume": str(volume),
    }

    result = _private_request("AddOrder", data)
    return result


def cancel_all_orders(pair: Optional[str] = None) -> Dict[str, Any]:
    """Annuleer alle open orders (of voor een specifiek pair)."""
    result = _private_request("CancelAll")
    return result


def get_open_orders() -> dict:
    """Haal alle open orders op van Kraken."""
    return _private_request("OpenOrders")


def get_stop_loss_level(symbol: str) -> Optional[float]:
    """
    Geef het huidige stop-loss niveau voor een coin.
    Zoekt in open orders naar een stop-loss sell order voor dit pair.
    """
    try:
        orders = get_open_orders()
        pair = find_usd_pair(symbol)
        if not pair:
            return None
        for order_id, order in orders.get("open", {}).items():
            descr = order.get("descr", {})
            order_pair = descr.get("pair", "").upper().replace("/", "")
            is_sl = descr.get("type") == "sell" and descr.get("ordertype") == "stop-loss"
            pair_clean = pair.upper().replace("/", "")
            if is_sl and (pair_clean in order_pair or order_pair in pair_clean):
                return float(order.get("stopprice", 0) or 0)
    except Exception:
        pass
    return None


def get_trailing_stop_order(symbol: str) -> Optional[dict]:
    """
    Controleer of er al een native trailing-stop order open staat voor dit symbool.
    Retourneert order-info dict als gevonden, anders None.
    """
    try:
        orders = get_open_orders()
        pair = find_usd_pair(symbol)
        if not pair:
            return None
        pair_clean = pair.upper().replace("/", "")
        for order_id, order in orders.get("open", {}).items():
            descr = order.get("descr", {})
            order_pair = descr.get("pair", "").upper().replace("/", "")
            is_ts = descr.get("type") == "sell" and descr.get("ordertype") == "trailing-stop"
            if is_ts and (pair_clean in order_pair or order_pair in pair_clean):
                return {
                    "order_id": order_id,
                    "stopprice": float(order.get("stopprice", 0) or 0),
                    "misc": order.get("misc", ""),
                }
    except Exception:
        pass
    return None


def place_native_trailing_stop(pair: str, volume: float, trail_pct: float = 0.20,
                               current_price: Optional[float] = None) -> Dict[str, Any]:
    """
    Plaats een NATIVE Kraken trailing-stop order.

    Kraken volgt de prijs real-time — stop schuift automatisch omhoog
    zonder dat de pipeline hoeft te draaien.

    trail_pct: trailing offset als fractie (0.20 = 20% onder de peak)
    current_price: huidige prijs (voor berekening absoluut trail bedrag).
                   Als niet opgegeven wordt de prijs live opgehaald.

    Kraken verwacht een absoluut prijsbedrag, geen percentage.
    """
    if current_price is None:
        ticker = get_ticker(pair)
        current_price = ticker.get("last", 0)

    trail_amount = round(current_price * trail_pct, 6)

    data = {
        "pair": pair,
        "type": "sell",
        "ordertype": "trailing-stop",
        "price": str(trail_amount),  # absoluut bedrag, bijv. "0.046300"
        "volume": str(volume),
    }
    result = _private_request("AddOrder", data)
    return result


def update_trailing_stop(symbol: str, current_price: float,
                         trail_pct: float = 0.20) -> dict:
    """
    Zorg dat er een native trailing-stop order actief is voor dit symbool.

    Strategie:
    - Al een native trailing-stop open → niets doen (Kraken beheert het real-time)
    - Nog geen trailing-stop → cancel alles (verwijder oude stop-loss) + plaats native

    Native trailing-stop schuift automatisch mee met de prijs op Kraken
    zonder dat de pipeline tussenbeide hoeft te komen.
    """
    result = {
        "symbol": symbol,
        "current_price": current_price,
        "trail_pct": trail_pct,
        "updated": False,
        "action": "none",
    }

    pair = find_usd_pair(symbol)
    if not pair:
        result["action"] = "no_pair"
        return result

    # Als er al een native trailing-stop staat → niets te doen
    existing = get_trailing_stop_order(symbol)
    if existing is not None:
        result["action"] = "no_change"
        result["existing_stop"] = existing.get("stopprice", 0)
        return result

    # Geen trailing-stop gevonden → cancel alles + plaats native trailing-stop
    # (cancel-first voorkomt EOrder:Insufficient funds door eventuele oude stop-loss)
    cancel_all_orders()
    time.sleep(2)

    balances = get_balance()
    volume = 0.0
    for asset, amount in balances.items():
        if symbol.upper() in asset.upper() and amount > 0:
            volume = amount
            break

    if volume > 0:
        place_native_trailing_stop(pair, volume, trail_pct)
        result["action"] = "placed"
        result["updated"] = True

    return result


def estimate_trade(pair: str, side: str, volume: float) -> Dict[str, Any]:
    """Schat een trade in zonder uit te voeren (voor bevestiging)."""
    ticker = get_ticker(pair)
    if not ticker:
        return {"error": "Kon prijs niet ophalen"}

    if side == "sell":
        est_usd = volume * ticker["bid"]
        fee_usd = est_usd * 0.0026  # 0.26% taker
        net_usd = est_usd - fee_usd
        return {
            "pair": pair,
            "side": "VERKOOP",
            "volume": volume,
            "price": ticker["bid"],
            "est_usd": round(est_usd, 2),
            "fee_usd": round(fee_usd, 2),
            "net_usd": round(net_usd, 2),
        }
    else:  # buy
        coins = volume / ticker["ask"]
        fee_usd = volume * 0.0026
        net_coins = (volume - fee_usd) / ticker["ask"]
        return {
            "pair": pair,
            "side": "KOOP",
            "usd_amount": volume,
            "price": ticker["ask"],
            "est_coins": round(net_coins, 6),
            "fee_usd": round(fee_usd, 2),
        }


# ---------------------------------------------------------------------------
# Volledige switch flow
# ---------------------------------------------------------------------------

def plan_switch(current_symbol: str, target_symbol: str) -> Dict[str, Any]:
    """
    Plan een switch van current_symbol naar target_symbol.
    Retourneert een trade-plan zonder uit te voeren.
    """
    # 1. Check balans
    balances = get_balance()

    # Zoek huidige coin in balans (Kraken gebruikt soms andere namen)
    current_amount = 0.0
    current_asset = None
    for asset, amount in balances.items():
        if current_symbol.upper() in asset.upper():
            current_amount = amount
            current_asset = asset
            break

    if current_amount <= 0:
        return {"error": f"Geen {current_symbol} in je account gevonden"}

    # 2. Vind pairs
    sell_pair = find_usd_pair(current_symbol)
    buy_pair = find_usd_pair(target_symbol)

    if not sell_pair:
        return {"error": f"Geen USD pair gevonden voor {current_symbol}"}
    if not buy_pair:
        return {"error": f"Geen USD pair gevonden voor {target_symbol}"}

    # 3. Schat trades in
    sell_est = estimate_trade(sell_pair, "sell", current_amount)
    if "error" in sell_est:
        return sell_est

    buy_est = estimate_trade(buy_pair, "buy", sell_est["net_usd"])
    if "error" in buy_est:
        return buy_est

    return {
        "current": current_symbol.upper(),
        "target": target_symbol.upper(),
        "step1_sell": sell_est,
        "step2_buy": buy_est,
        "total_fee_usd": round(sell_est["fee_usd"] + buy_est["fee_usd"], 2),
        "summary": (
            f"VERKOOP {current_amount:.4f} {current_symbol.upper()} "
            f"(~${sell_est['est_usd']:.2f}) → "
            f"KOOP ~{buy_est['est_coins']:.4f} {target_symbol.upper()} "
            f"(fee: ${sell_est['fee_usd'] + buy_est['fee_usd']:.2f})"
        ),
    }


def execute_switch(current_symbol: str, target_symbol: str, max_usd: Optional[float] = None) -> Dict[str, Any]:
    """
    Voer een switch uit: verkoop current → USD → koop target.
    ALLEEN aanroepen na bevestiging van de gebruiker!
    """
    # 1. Balans checken
    balances = get_balance()
    current_amount = 0.0
    for asset, amount in balances.items():
        if current_symbol.upper() in asset.upper():
            current_amount = amount
            break

    if current_amount <= 0:
        return {"error": f"Geen {current_symbol} gevonden in account"}

    # 2. Annuleer open orders zodat coins niet geblokkeerd zijn (bijv. stop-loss)
    try:
        print(f"[Kraken] Annuleer open orders voor {current_symbol}...")
        cancel_all_orders()
        time.sleep(2)  # Wacht tot annulering verwerkt is op Kraken
    except Exception as e:
        print(f"[Kraken] Waarschuwing: orders annuleren mislukt: {e}")

    # 3. Verkoop naar USD
    sell_pair = find_usd_pair(current_symbol)
    if not sell_pair:
        return {"error": f"Geen USD pair voor {current_symbol}"}

    print(f"[Kraken] Verkoop {current_amount} {current_symbol} via {sell_pair}...")
    sell_result = place_market_order(sell_pair, "sell", current_amount)
    sell_txids = sell_result.get("txid", [])
    print(f"[Kraken] Verkoop order geplaatst: {sell_txids}")

    # Wacht even tot order is gevuld
    time.sleep(3)

    # 3. Check hoeveel USD we nu hebben
    balances = get_balance()
    usd_available = 0.0
    for asset, amount in balances.items():
        if asset in ("ZUSD", "USD"):
            usd_available = amount
            break

    if usd_available < 1.0:
        return {"error": "Verkoop lijkt niet gelukt — geen USD beschikbaar",
                "sell_txids": sell_txids}

    # 4. Koop target met beschikbare USD
    buy_pair = find_usd_pair(target_symbol)
    if not buy_pair:
        return {"error": f"Geen USD pair voor {target_symbol}",
                "sell_txids": sell_txids, "usd_available": usd_available}

    # Respecteer max_usd limiet (bijv. bij CAUTIOUS: 50% van totaal)
    buy_usd = min(usd_available, max_usd) if max_usd else usd_available
    print(f"[Kraken] Koop {target_symbol} met ${buy_usd:.2f} via {buy_pair}...")
    buy_result = place_market_order(buy_pair, "buy", buy_usd)
    buy_txids = buy_result.get("txid", [])
    print(f"[Kraken] Koop order geplaatst: {buy_txids}")

    return {
        "status": "COMPLETED",
        "sold": f"{current_amount} {current_symbol}",
        "bought": f"{target_symbol} met ${usd_available:.2f}",
        "sell_txids": sell_txids,
        "buy_txids": buy_txids,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    import argparse
    ap = argparse.ArgumentParser(description="Kraken trading module")
    ap.add_argument("--balance", action="store_true", help="Toon account balans")
    ap.add_argument("--plan", nargs=2, metavar=("FROM", "TO"),
                    help="Plan een switch (bijv. --plan RIVER SOL)")
    ap.add_argument("--execute", nargs=2, metavar=("FROM", "TO"),
                    help="Voer een switch uit (ALLEEN na bevestiging!)")
    args = ap.parse_args()

    if args.balance:
        print("Kraken Account Balans:")
        print(get_balance_summary())

    if args.plan:
        plan = plan_switch(args.plan[0], args.plan[1])
        if "error" in plan:
            print(f"Fout: {plan['error']}")
        else:
            print(f"\nTrade Plan:")
            print(f"  {plan['summary']}")
            print(f"  Totale fee: ${plan['total_fee_usd']}")

    if args.execute:
        result = execute_switch(args.execute[0], args.execute[1])
        if "error" in result:
            print(f"Fout: {result['error']}")
        else:
            print(f"Trade uitgevoerd: {result}")


if __name__ == "__main__":
    main()
