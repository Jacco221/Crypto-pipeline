#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$HOME/Crypto-pipeline"
REPORTS_DIR="$REPO_DIR/data/reports"
DESKTOP_DIR="$HOME/Desktop/crypto-reports"
HISTORY_DIR="$DESKTOP_DIR/history"
STAMP=$(TZ=Europe/Amsterdam date +%Y-%m-%d)

mkdir -p "$REPORTS_DIR" "$DESKTOP_DIR" "$HISTORY_DIR"
echo "▶️  Run pipeline (NL datum: $STAMP)"
cd "$REPO_DIR"

# 1) Scores (CoinGecko)
python3 scripts/build_scores.py

# 2) Scores → Kraken-only
python3 scripts/filter_kraken.py \
  --scores-csv "$REPORTS_DIR/scores_latest.csv" \
  --out-csv    "$REPORTS_DIR/scores_kraken_latest.csv" \
  --out-md     "$REPORTS_DIR/scores_kraken_latest.md" \
  --quotes USD,EUR,USDT,USDC --top 200 --exclude-top-rank 30 --exclude-bluechips

# 3a) Moonshots v2 (algemeen universum)
python3 scripts/moonshot_v2.py \
  --scores-csv "$REPORTS_DIR/scores_latest.csv" \
  --out-csv    "$REPORTS_DIR/moonshots_v2_latest.csv" \
  --out-md     "$REPORTS_DIR/moonshots_v2_latest.md" \
  --top 10 --exclude-top-rank 30 --exclude-bluechips --quotes USD,EUR,USDT,USDC

# 3b) Moonshots (Kraken-only)
python3 scripts/moonshot_v2.py \
  --scores-csv "$REPORTS_DIR/scores_latest.csv" \
  --out-csv    "$REPORTS_DIR/moonshots_kraken_latest.csv" \
  --out-md     "$REPORTS_DIR/moonshots_kraken_latest.md" \
  --top 10 --exclude-top-rank 30 --exclude-bluechips \
  --kraken-only --quotes USD,EUR,USDT,USDC

# 4) Market regime + cooldown
python3 scripts/annotate_market_regime.py --out-md "$REPORTS_DIR/top5_latest.md" --window 20 --days 120
python3 scripts/cooldown_guard.py --md "$REPORTS_DIR/top5_latest.md"

# 5) Top-5 CSV voor allocatie
python3 scripts/build_top5_csv.py \
  --scores-csv "$REPORTS_DIR/scores_latest.csv" \
  --out-csv    "$REPORTS_DIR/top5_latest.csv" \
  --exclude-top-rank 30 --exclude-bluechips --top 5

# 6) Allocatie
python3 scripts/advise_allocation.py \
  --top5 "$REPORTS_DIR/top5_latest.csv" \
  --out  "$REPORTS_DIR/allocation_latest.json" \
  --append-md --md-file "$REPORTS_DIR/top5_latest.md"

# 7) Archiveren naar Desktop + history
bash scripts/archive_reports.sh "$REPORTS_DIR" "$DESKTOP_DIR"

# extra: push belangrijkste sets rechtstreeks mee
cp -f "$REPORTS_DIR"/scores_kraken_latest.*            "$DESKTOP_DIR"/ || true
cp -f "$REPORTS_DIR"/moonshots_v2_latest.*             "$DESKTOP_DIR"/ || true
cp -f "$REPORTS_DIR"/moonshots_kraken_latest.*         "$DESKTOP_DIR"/ || true

cp -f "$REPORTS_DIR"/scores_kraken_latest.csv          "$HISTORY_DIR/scores_kraken_${STAMP}.csv"        || true
cp -f "$REPORTS_DIR"/scores_kraken_latest.md           "$HISTORY_DIR/scores_kraken_${STAMP}.md"         || true
cp -f "$REPORTS_DIR"/moonshots_v2_latest.csv           "$HISTORY_DIR/moonshots_v2_${STAMP}.csv"         || true
cp -f "$REPORTS_DIR"/moonshots_v2_latest.md            "$HISTORY_DIR/moonshots_v2_${STAMP}.md"          || true
cp -f "$REPORTS_DIR"/moonshots_kraken_latest.csv       "$HISTORY_DIR/moonshots_kraken_${STAMP}.csv"     || true
cp -f "$REPORTS_DIR"/moonshots_kraken_latest.md        "$HISTORY_DIR/moonshots_kraken_${STAMP}.md"      || true

echo "✅ Klaar. Bekijk: $DESKTOP_DIR  en  $HISTORY_DIR"
