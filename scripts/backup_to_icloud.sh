#!/usr/bin/env bash
set -euo pipefail

# Locaties
PROJ="$HOME/Crypto-pipeline"
ICLOUD_ROOT="$HOME/Library/Mobile Documents/com~apple~CloudDocs"
ICLOUD_DIR="$ICLOUD_ROOT/crypto-backups"
DATE="$(date -u +%Y-%m-%d_%H%M%SZ)"
SNAP="$ICLOUD_DIR/snapshots/$DATE"

echo "==> iCloud backup naar: $SNAP"
mkdir -p "$SNAP"

# 1) Code + configuratie + workflows + scripts + data (history)
#    We nemen .git mee (handig voor volledig herstel), maar speigelen verder selectief.
rsync -av --delete \
  --include ".git/***" \
  --include ".github/***" \
  --include "scripts/***" \
  --include "src/***" \
  --include "data/reports/***" \
  --include "requirements.txt" \
  --include "run.py" \
  --include "README.md" \
  --include "pipeline_trades*.xlsx" \
  --include ".gitignore" \
  --include "pull_reports.sh" \
  --include "scripts/*.py" \
  --exclude "*" \
  "$PROJ"/ "$SNAP"/

# 2) Ook de eindrapportmap die we lokaal op Desktop bijhouden (als die bestaat)
if [ -d "$HOME/Desktop/crypto-reports" ]; then
  echo "==> Kopieer Desktop/crypto-reports mee"
  rsync -av "$HOME/Desktop/crypto-reports"/ "$SNAP/crypto-reports_desktop"/
fi

# 3) Als je ook via iCloud al een crypto-reports-map hebt, kopieer die mee
if [ -d "$ICLOUD_ROOT/crypto-reports" ]; then
  echo "==> Kopieer iCloud/crypto-reports mee"
  rsync -av "$ICLOUD_ROOT/crypto-reports"/ "$SNAP/crypto-reports_icloud"/
fi

# 4) Archief (.tgz) maken van deze snapshot
mkdir -p "$ICLOUD_DIR/archives"
ARCHIVE="$ICLOUD_DIR/archives/crypto-pipeline_${DATE}.tgz"
echo "==> Maak archief: $ARCHIVE"
tar -C "$SNAP/.." -czf "$ARCHIVE" "$(basename "$SNAP")"

# 5) Bewaar alleen laatste 7 archieven
echo "==> Opruimen oude archieven (behoud 7 nieuwste)"
ls -1t "$ICLOUD_DIR/archives"/crypto-pipeline_*.tgz 2>/dev/null | tail -n +8 | xargs -r rm -f

echo "✅ Klaar. Snapshot: $SNAP"
