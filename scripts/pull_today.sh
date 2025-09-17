#!/usr/bin/env bash
set -euo pipefail

WF="pipeline.yml"
DESK="$HOME/Desktop/crypto-reports"
TMP="/tmp/reports_today"
TODAY=$(date -u +%Y-%m-%d)   # gebruik UTC-datum in bestandsnamen
HIST="$DESK/history/$TODAY"

echo "▶️  Start nieuwe workflow-run op 'main'…"
gh workflow run "$WF" --ref main >/dev/null

echo "⏳ Wacht op completion (poll elke 10s)…"
while true; do
  STATUS_JSON=$(gh run list --workflow="$WF" --limit 1 --json databaseId,status,conclusion,createdAt 2>/dev/null)
  RUN_ID=$(echo "$STATUS_JSON" | jq -r '.[0].databaseId')
  RUN_STATUS=$(echo "$STATUS_JSON" | jq -r '.[0].status')
  RUN_CONCL=$(echo "$STATUS_JSON" | jq -r '.[0].conclusion')
  printf "   • RUN_ID=%s  status=%s  conclusion=%s\r" "$RUN_ID" "$RUN_STATUS" "$RUN_CONCL"
  if [ "$RUN_STATUS" = "completed" ]; then
    echo
    break
  fi
  sleep 10
done

if [ "$RUN_CONCL" != "success" ]; then
  echo "❌ Run $RUN_ID niet succesvol (conclusion=$RUN_CONCL). Bekijk logs in GitHub Actions."
  exit 1
fi
echo "✅ Run $RUN_ID succesvol afgerond."

echo "📥 Download artifact 'reports'…"
rm -rf "$TMP"; mkdir -p "$TMP"
gh run download "$RUN_ID" -n reports -D "$TMP"

echo "🗂  Plaats op Desktop en archiveer…"
mkdir -p "$DESK" "$HIST"
# Kopieer alles naar Desktop (vervangt 'latest*' en dagbestanden)
cp -r "$TMP"/* "$DESK"/
# Verplaats dagbestanden van vandaag naar history-snapshot
shopt -s nullglob
mv "$DESK"/*"$TODAY"* "$HIST"/ || true
# Bewaar ook een kopie van 'latest*' in history-snapshot
cp "$DESK"/latest* "$HIST"/ 2>/dev/null || true

echo "🔎 Controle:"
ls -lh "$DESK" | sed -n '1,200p'
echo "—"
echo "📚 History-map: $HIST"
ls -lh "$HIST" | sed -n '1,200p'

echo "🎉 Klaar."
