#!/bin/bash
set -euo pipefail

# === Config ===
REPO="Jacco221/Crypto-pipeline"
DEST_DESK=~/Desktop/crypto-reports
DEST_ICLOUD=~/Library/Mobile\ Documents/com~apple~CloudDocs/crypto-reports
TMP=/tmp/reports_latest

echo "✅ Repo: $REPO"
echo "✅ Output: $DEST_DESK"
echo "✅ iCloud map gedetecteerd: $DEST_ICLOUD"

# === Maak output directories aan ===
mkdir -p "$DEST_DESK"
mkdir -p "$DEST_ICLOUD"
rm -rf "$TMP" && mkdir -p "$TMP"

# === Bepaal laatste succesvolle run-ID ===
echo "⏳ Zoek laatste succesvolle run..."
RUN_ID=$(gh run list --repo "$REPO" --workflow="pipeline.yml" \
  --limit 1 --json databaseId,status,conclusion \
  --jq '.[] | select(.status=="completed" and .conclusion=="success") | .databaseId')

if [ -z "$RUN_ID" ]; then
  echo "❌ Geen succesvolle run gevonden!"
  exit 1
fi

echo "✅ Laatste succesvolle run: $RUN_ID (datum: $(date +%Y-%m-%d))"

# === Download artifacts ===
echo "⏳ Download artifacts..."
gh run download "$RUN_ID" --repo "$REPO" -n reports -D "$TMP"

# === Kopieer naar Desktop en iCloud ===
echo "⏳ Kopieer naar Desktop en iCloud..."
rsync -ah "$TMP"/ "$DEST_DESK"/
rsync -ah "$TMP"/ "$DEST_ICLOUD"/

echo "✅ Klaar! Rapporten staan in:"
echo "   - $DEST_DESK"
echo "   - $DEST_ICLOUD"

