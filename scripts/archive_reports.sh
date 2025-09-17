#!/bin/bash
set -euo pipefail

SRC_DIR="$1"                         # bv. ~/Crypto-pipeline/data/reports
DST_DIR="$2"                         # bv. ~/Desktop/crypto-reports
HISTORY_DIR="$DST_DIR/history"
STAMP=$(TZ=Europe/Amsterdam date +%Y-%m-%d)

mkdir -p "$DST_DIR" "$HISTORY_DIR"

copy_if_exists() {
  local file="$1"
  local target="$2"
  if [ -f "$file" ]; then
    cp -f "$file" "$target"
  fi
}

# 1) Laatste versies naar Desktop (zichtbaar in Finder)
for f in \
  scores_latest.csv scores_latest.json \
  top5_latest.md latest.csv latest.json \
  scores_kraken_latest.csv scores_kraken_latest.md \
  moonshots_v2_latest.csv moonshots_v2_latest.md \
  moonshots_kraken_latest.csv moonshots_kraken_latest.md \
  allocation_latest.json
do
  copy_if_exists "$SRC_DIR/$f" "$DST_DIR/$f"
done

# 2) Gearchiveerde kopieën met datum
copy_if_exists "$SRC_DIR/scores_latest.csv"           "$HISTORY_DIR/scores_${STAMP}.csv"
copy_if_exists "$SRC_DIR/scores_latest.json"          "$HISTORY_DIR/scores_${STAMP}.json"
copy_if_exists "$SRC_DIR/top5_latest.md"              "$HISTORY_DIR/top5_${STAMP}.md"
copy_if_exists "$SRC_DIR/latest.csv"                  "$HISTORY_DIR/${STAMP}_latest.csv"
copy_if_exists "$SRC_DIR/latest.json"                 "$HISTORY_DIR/${STAMP}_latest.json"
copy_if_exists "$SRC_DIR/scores_kraken_latest.csv"    "$HISTORY_DIR/scores_kraken_${STAMP}.csv"
copy_if_exists "$SRC_DIR/scores_kraken_latest.md"     "$HISTORY_DIR/scores_kraken_${STAMP}.md"
copy_if_exists "$SRC_DIR/moonshots_v2_latest.csv"     "$HISTORY_DIR/moonshots_v2_${STAMP}.csv"
copy_if_exists "$SRC_DIR/moonshots_v2_latest.md"      "$HISTORY_DIR/moonshots_v2_${STAMP}.md"
copy_if_exists "$SRC_DIR/moonshots_kraken_latest.csv" "$HISTORY_DIR/moonshots_kraken_${STAMP}.csv"
copy_if_exists "$SRC_DIR/moonshots_kraken_latest.md"  "$HISTORY_DIR/moonshots_kraken_${STAMP}.md"
copy_if_exists "$SRC_DIR/allocation_latest.json"      "$HISTORY_DIR/allocation_${STAMP}.json"

echo "✅ Klaar. Nieuwe rapporten staan in: $DST_DIR en archief: $HISTORY_DIR"

