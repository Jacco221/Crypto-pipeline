# src/dip_finder.py
"""
Dip Finder — detecteert coins met sterke dalingen zonder slecht nieuws.

Contraire instap-strategie: koop de dip als:
1. Coin daalt meer dan de markt (isolated dip, niet systemic)
2. Volume is niet abnormaal hoog (geen paniekverkoop door nieuws)
3. Sparkline toont eerste tekenen van herstel
4. Fear & Greed is laag (algemene angst, geen coin-specifiek probleem)
5. Ver van ATH (meer upside potentie)

Gebruikt dezelfde bulk-data als pipeline.py — 0 extra API calls.
Standalone: python3 -m src.dip_finder --limit 150
"""
from __future__ import annotations

import argparse
import math
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from src.pipeline import _fetch_markets_bulk
from src.sentiment import fear_greed_index
from src.ta import _clamp01, weighted_group_score


# ---------------------------------------------------------------------------
# Filter: welke coins komen in aanmerking als dip-kandidaat?
# ---------------------------------------------------------------------------

MIN_MCAP = 50_000_000       # $50M minimum market cap
MIN_VOLUME_24H = 1_000_000  # $1M minimum 24h volume
DIP_24H_THRESHOLD = -8.0    # minimaal -8% in 24h
DIP_7D_THRESHOLD = -15.0    # OF minimaal -15% in 7d


def _is_dip_candidate(coin: dict, btc_24h: float, btc_7d: float) -> bool:
    """Check of een coin voldoet aan de harde dip-criteria."""
    mcap = coin.get("market_cap") or 0
    volume = coin.get("total_volume") or 0
    if mcap < MIN_MCAP or volume < MIN_VOLUME_24H:
        return False

    change_24h = coin.get("price_change_percentage_24h_in_currency") or 0.0
    change_7d = coin.get("price_change_percentage_7d_in_currency") or 0.0

    # Moet significant gedaald zijn
    has_dip = change_24h <= DIP_24H_THRESHOLD or change_7d <= DIP_7D_THRESHOLD
    if not has_dip:
        return False

    # Moet meer gedaald zijn dan BTC (isolated, niet systemic)
    isolated_24h = change_24h < btc_24h - 3.0  # minimaal 3pp slechter dan BTC
    isolated_7d = change_7d < btc_7d - 5.0     # minimaal 5pp slechter dan BTC
    if not (isolated_24h or isolated_7d):
        return False

    return True


# ---------------------------------------------------------------------------
# Dip subscores
# ---------------------------------------------------------------------------

def _isolated_dip_score(coin: dict, btc_24h: float, btc_7d: float) -> float:
    """
    Hoe geïsoleerd is de dip? Grotere afwijking van BTC = hoger signaal.
    Coin -20% terwijl BTC -2% = sterk geïsoleerd (score ~0.8)
    Coin -10% terwijl BTC -8% = nauwelijks geïsoleerd (score ~0.2)
    """
    change_24h = coin.get("price_change_percentage_24h_in_currency") or 0.0
    change_7d = coin.get("price_change_percentage_7d_in_currency") or 0.0

    # Neem het sterkste isolatie-signaal
    iso_24h = btc_24h - change_24h  # positief getal = coin deed het slechter
    iso_7d = btc_7d - change_7d
    max_iso = max(iso_24h, iso_7d)

    # Map: 5pp verschil -> 0.3, 15pp -> 0.6, 30pp+ -> 0.9
    return _clamp01(max_iso / 35.0)


def _volume_normality_score(coin: dict) -> float:
    """
    Is het volume normaal (geen nieuwsgedreven paniek)?
    Laag volume/mcap ratio tijdens dip = waarschijnlijk geen nieuws.
    Hoog volume = mogelijk nieuws-gedreven sell-off.

    Typische vol/mcap ratio: 0.01-0.10 normaal, >0.20 = hoog
    """
    volume = coin.get("total_volume") or 0
    mcap = coin.get("market_cap") or 1
    ratio = volume / mcap

    # Inverteer: laag volume = hoge score (geen paniek)
    # ratio 0.02 -> score 0.9 (normaal)
    # ratio 0.10 -> score 0.5 (gemiddeld)
    # ratio 0.30 -> score 0.1 (paniek/nieuws)
    return _clamp01(1.0 - (ratio / 0.25))


def _recovery_score(coin: dict) -> float:
    """
    Toont de sparkline tekenen van herstel na de dip?
    Vergelijk de laatste 12 uur met het dieptepunt van de week.
    """
    sparkline = coin.get("sparkline_in_7d", {}).get("price", [])
    if len(sparkline) < 48:  # minimaal 2 dagen data
        return 0.5

    prices = np.array(sparkline, dtype=float)

    # Dieptepunt van de hele sparkline
    min_price = np.min(prices)
    min_idx = np.argmin(prices)

    # Huidige prijs (laatste punt)
    current = prices[-1]

    if min_price <= 0:
        return 0.5

    # Hoeveel is het hersteld vanaf het dieptepunt?
    recovery_pct = (current - min_price) / min_price

    # Bonus: dieptepunt was recent (laatste 48h = index > 120)
    # Dit betekent dat de dip VERS is en herstel net begint
    recency_bonus = 0.0
    if min_idx > len(prices) - 48:
        recency_bonus = 0.15

    # Map: 0% recovery -> 0.1, 5% recovery -> 0.5, 15%+ -> 0.9
    base = _clamp01(recovery_pct / 0.15)

    return _clamp01(base + recency_bonus)


def _ath_upside_score(coin: dict) -> float:
    """
    Hoe ver van ATH? Verder = meer potentiële upside.
    -30% van ATH -> score 0.3
    -60% van ATH -> score 0.6
    -90% van ATH -> score 0.9
    """
    ath_change = coin.get("ath_change_percentage") or 0.0
    # ath_change is negatief (bijv. -46% onder ATH)
    distance = abs(ath_change)
    return _clamp01(distance / 100.0)


# ---------------------------------------------------------------------------
# Gecombineerde dip score
# ---------------------------------------------------------------------------

def score_dip(coin: dict, btc_24h: float, btc_7d: float,
              fg_score: float) -> Dict[str, float]:
    """Bereken de gecombineerde dip-score voor een coin."""
    iso = _isolated_dip_score(coin, btc_24h, btc_7d)
    vol = _volume_normality_score(coin)
    rec = _recovery_score(coin)
    ath = _ath_upside_score(coin)

    scores = {
        "isolated_dip": iso,
        "volume_normal": vol,
        "recovery": rec,
        "ath_upside": ath,
        "fear_greed": fg_score,
    }
    weights = {
        "isolated_dip": 0.35,
        "volume_normal": 0.20,
        "recovery": 0.25,
        "ath_upside": 0.10,
        "fear_greed": 0.10,
    }

    total = weighted_group_score(scores, weights)

    symbol = (coin.get("symbol") or "").upper()
    change_24h = coin.get("price_change_percentage_24h_in_currency") or 0.0
    change_7d = coin.get("price_change_percentage_7d_in_currency") or 0.0

    return {
        "symbol": symbol,
        "name": coin.get("name", symbol),
        "price": coin.get("current_price", 0),
        "mcap_M": round((coin.get("market_cap") or 0) / 1e6, 1),
        "chg_24h_%": round(change_24h, 1),
        "chg_7d_%": round(change_7d, 1),
        "ath_dist_%": round(coin.get("ath_change_percentage", 0), 1),
        "iso_dip": round(iso, 3),
        "vol_norm": round(vol, 3),
        "recovery": round(rec, 3),
        "ath_up": round(ath, 3),
        "dip_score": round(total, 3),
    }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def find_dips(limit: int = 150, top_n: int = 10) -> pd.DataFrame:
    """
    Vind top dip-kansen. Gebruikt bulk data (0 extra API calls).

    Returns DataFrame met top_n dip-kandidaten, gesorteerd op dip_score.
    """
    print(f"[DipFinder] Scanning top {limit} coins voor dip-kansen...")
    start = time.time()

    # 1. Bulk data (hergebruikt zelfde call als pipeline)
    coins = _fetch_markets_bulk(limit=limit)
    print(f"[DipFinder] {len(coins)} coins geladen")

    # 2. BTC benchmark
    btc = next((c for c in coins if (c.get("symbol") or "").lower() == "btc"), None)
    btc_24h = btc.get("price_change_percentage_24h_in_currency", 0.0) if btc else 0.0
    btc_7d = btc.get("price_change_percentage_7d_in_currency", 0.0) if btc else 0.0
    print(f"[DipFinder] BTC benchmark: 24h={btc_24h:+.1f}%, 7d={btc_7d:+.1f}%")

    # 3. Fear & Greed (1 API call)
    fg_score, _ = fear_greed_index()
    fg_value = int((1.0 - fg_score) * 100)
    print(f"[DipFinder] Fear & Greed: {fg_value}/100 (score: {fg_score:.2f})")

    # 4. Filter kandidaten
    candidates = [c for c in coins if _is_dip_candidate(c, btc_24h, btc_7d)]
    print(f"[DipFinder] {len(candidates)} dip-kandidaten gevonden")

    if not candidates:
        print("[DipFinder] Geen dips gevonden die aan criteria voldoen.")
        return pd.DataFrame()

    # 5. Score alle kandidaten
    results = [score_dip(c, btc_24h, btc_7d, fg_score) for c in candidates]
    df = pd.DataFrame(results)
    df = df.sort_values("dip_score", ascending=False).head(top_n).reset_index(drop=True)

    elapsed = time.time() - start
    print(f"[DipFinder] Klaar in {elapsed:.1f}s — top {len(df)} dips")

    return df


def write_dip_reports(df: pd.DataFrame, reports_dir: Path) -> None:
    """Schrijf dip rapport als MD en CSV."""
    reports_dir.mkdir(parents=True, exist_ok=True)

    if df.empty:
        md = "# Dip Finder\n\nGeen dip-kansen gevonden die aan criteria voldoen.\n"
        (reports_dir / "dips_latest.md").write_text(md, encoding="utf-8")
        return

    # CSV
    df.to_csv(reports_dir / "dips_latest.csv", index=False)

    # Markdown
    lines = []
    lines.append(f"# Dip Finder (gegenereerd: {time.strftime('%Y-%m-%d %H:%M:%S')})\n")
    lines.append("Coins met sterke daling maar zonder tekenen van slecht nieuws.\n")
    lines.append("**Strategie:** Contrair instappen — koop de dip als het angstgedreven is.\n")

    lines.append("\n## Criteria")
    lines.append(f"- Daling >= {abs(DIP_24H_THRESHOLD)}% (24h) OF >= {abs(DIP_7D_THRESHOLD)}% (7d)")
    lines.append(f"- Market cap > ${MIN_MCAP/1e6:.0f}M")
    lines.append(f"- Volume > ${MIN_VOLUME_24H/1e6:.0f}M/24h")
    lines.append("- Dip is groter dan BTC (geïsoleerd, niet marktbreed)\n")

    lines.append("## Top Dip-kansen\n")
    # Selecteer kolommen voor leesbaarheid
    display = df[["symbol", "name", "price", "chg_24h_%", "chg_7d_%",
                   "ath_dist_%", "dip_score"]].copy()
    display.columns = ["Symbol", "Naam", "Prijs", "24h%", "7d%", "ATH%", "Dip Score"]
    lines.append(display.to_markdown(index=False))

    lines.append("\n## Score-uitleg")
    lines.append("| Component | Gewicht | Betekenis |")
    lines.append("|-----------|---------|-----------|")
    lines.append("| Isolated dip | 35% | Hoeveel slechter dan BTC (groter = sterker signaal) |")
    lines.append("| Volume normaal | 20% | Laag volume = geen nieuws-paniek |")
    lines.append("| Herstel | 25% | Stijgt de prijs al vanaf het dieptepunt? |")
    lines.append("| ATH afstand | 10% | Ver van ATH = meer upside potentie |")
    lines.append("| Fear & Greed | 10% | Lage F&G = marktbrede angst (contrair positief) |")

    lines.append("\n> **Let op:** Dit is geen koopadvies. Controleer altijd het nieuws en fundamentals.")

    (reports_dir / "dips_latest.md").write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Dip Finder: vind dip-kansen zonder slecht nieuws")
    ap.add_argument("--limit", type=int, default=150, help="Aantal coins om te scannen")
    ap.add_argument("--top", type=int, default=10, help="Aantal top dips in rapport")
    ap.add_argument("--reports-dir", type=str, default="data/reports", help="Output directory")
    args = ap.parse_args()

    df = find_dips(limit=args.limit, top_n=args.top)
    write_dip_reports(df, Path(args.reports_dir))

    if not df.empty:
        print(f"\nTop {len(df)} dip-kansen:")
        for _, row in df.iterrows():
            print(f"  {row['symbol']:>8} | 24h: {row['chg_24h_%']:+6.1f}% | "
                  f"7d: {row['chg_7d_%']:+6.1f}% | score: {row['dip_score']:.3f}")


if __name__ == "__main__":
    main()
