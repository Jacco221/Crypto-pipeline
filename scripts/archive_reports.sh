#!/bin/bash
set -euo pipefail

# Mapinstellingen
SRC_DIR="$1"                         # projectmap: bv. ~/Crypto-pipeline/data/reports
DST_DIR="$2"                         # Desktop-map: bv. ~/Desktop/crypto-reports
HISTORY_DIR="$DST_DIR/history"
STAMP=$(date +%Y-%m-%d)

mkdir -p "$DST_DIR" "$HISTORY_DIR"

copy_if_exists() {
  local file="$1"
  local target="$2"
  if [ -f "$file" ]; then
    cp -f "$file" "$target"
  fi
}

# 1) Kopieer laatste versies naar hoofdfolder (zodat je altijd “vandaag” ziet in Finder)
copy_if_exists "$SRC_DIR/scores_latest.csv" "$DST_DIR/scores_latest.csv"
copy_if_exists "$SRC_DIR/scores_latest.json" "$DST_DIR/scores_latest.json"
copy_if_exists "$SRC_DIR/top5_latest.md"     "$DST_DIR/top5_latest.md"
copy_if_exists "$SRC_DIR/latest.csv"         "$DST_DIR/latest.csv"
copy_if_exists "$SRC_DIR/latest.json"        "$DST_DIR/latest.json"

# 2) Maak gearchiveerde kopieën met datum in history/
copy_if_exists "$SRC_DIR/scores_latest.csv" "$HISTORY_DIR/scores_${STAMP}.csv"
copy_if_exists "$SRC_DIR/scores_latest.json" "$HISTORY_DIR/scores_${STAMP}.json"
copy_if_exists "$SRC_DIR/top5_latest.md"     "$HISTORY_DIR/top5_${STAMP}.md"
copy_if_exists "$SRC_DIR/latest.csv"         "$HISTORY_DIR/${STAMP}_latest.csv"
copy_if_exists "$SRC_DIR/latest.json"        "$HISTORY_DIR/${STAMP}_latest.json"

echo "✅ Klaar. Nieuwe rapporten staan in: $DST_DIR (vandaag) en $HISTORY_DIR (archief)"

