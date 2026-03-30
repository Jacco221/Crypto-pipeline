# src/universe.py
from src.utils import get

STABLES = {"usdt", "usdc", "busd", "dai", "tusd", "usde", "usdp"}

def get_top_coins(limit: int = 20, exclude_stables: bool = True):
    """
    Haalt top coins op via CoinGecko 'markets' endpoint gesorteerd op market cap.
    Retourneert lijst dicts met: id, symbol (UPPER), name.
    """
    url = "https://api.coingecko.com/api/v3/coins/markets"
    per_page = min(limit, 250)  # CG kan tot 250 per pagina
    params = {
        "vs_currency": "usd",
        "order": "market_cap_desc",
        "per_page": per_page,
        "page": 1,
        "sparkline": "false",
        "price_change_percentage": "1h,24h,7d",
    }

    data = get(url, params=params)  # robuuste GET met backoff (uit src/utils.py)

    coins = []
    for d in data:
        sym = (d.get("symbol") or "").upper()
        if exclude_stables and sym.lower() in STABLES:
            continue
        coins.append({
            "id": d["id"],
            "symbol": sym,
            "name": d.get("name", sym),
        })
        if len(coins) >= limit:
            break

    if not coins:
        raise RuntimeError("get_top_coins() gaf geen munten terug — rate limit of API-wijziging?")
    return coins
