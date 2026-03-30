from src.utils import get
import pandas as pd

def fetch_btc_price():
    """
    Probeert in volgorde meerdere providers om blokkades (451) of downtime te omzeilen.
    Retourneert een DataFrame met symbol en USD-prijs.
    """
    # 1) Binance (kan 451 geven op GitHub runners)
    try:
        data = get("https://api.binance.com/api/v3/ticker/price", params={"symbol": "BTCUSDT"})
        price = float(data["price"])
        return pd.DataFrame([{"symbol": "BTC", "price_usd": price, "source": "binance"}])
    except Exception as e:
        last_err = e

    # 2) CoinGecko (geen API-key; rate limits mogelijk bij veel runs)
    try:
        data = get("https://api.coingecko.com/api/v3/simple/price", params={"ids":"bitcoin","vs_currencies":"usd"})
        price = float(data["bitcoin"]["usd"])
        return pd.DataFrame([{"symbol": "BTC", "price_usd": price, "source": "coingecko"}])
    except Exception as e:
        last_err = e

    # 3) Coinbase
    try:
        data = get("https://api.coinbase.com/v2/prices/BTC-USD/spot")
        price = float(data["data"]["amount"])
        return pd.DataFrame([{"symbol": "BTC", "price_usd": price, "source": "coinbase"}])
    except Exception as e:
        last_err = e

    # Alles faalde:
    raise RuntimeError(f"All price providers failed. Last error: {last_err}")
    
if __name__ == "__main__":
    df = fetch_btc_price()
    print(df)
