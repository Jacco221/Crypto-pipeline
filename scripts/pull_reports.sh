#!/usr/bin/env bash
set -euo pipefail

REPO="Jacco221/Crypto-pipeline"
WORKFLOW_FILE="pipeline.yml"

DESK="$HOME/Desktop/crypto-reports"
ICLOUD="$HOME/Library/Mobile Documents/com~apple~CloudDocs/crypto-reports"
HIST="$DESK/history"
IHIST="$ICLOUD/history"

RETRIES=4
SLEEP_SECS=60

log()  { printf '[%s] %s\n' "$(date -u +%F' '%T'Z')" "$*"; }
warn() { printf '[%s] ⚠ %s\n' "$(date -u +%F' '%T'Z')" "$*" >&2; }
err()  { printf '[%s] ❌ %s\n' "$(date -u +%F' '%T'Z')" "$*" >&2; }

latest_success_run_id() {
  gh run list --workflow="$WORKFLOW_FILE" -L 20 --json databaseId,status,conclusion \
  | jq -r '[.[] | select(.status=="completed" and .conclusion=="success")][0].databaseId // empty'
}

artifact_name_for_run() {
  local run_id="$1"
  gh api "repos/$REPO/actions/runs/$run_id/artifacts" --jq '.artifacts[].name' | head -n1
}

download_artifact() {
  local run_id="$1"; local name="$2"; local dest="$3"
  gh run download "$run_id" --repo "$REPO" --name "$name" --dir "$dest"
}

write_manifest() {
  local outdir="$1"
  {
    echo "run_id=$RUN_ID"
    echo "written_utc=$(date -u +%FT%TZ)"
    gh run view "$RUN_ID" --repo "$REPO" --json status,conclusion,createdAt,updatedAt
  } > "$outdir/manifest.txt"
}

copy_latest_if_exists() {
  local src="$1"; local dst="$2"
  local files=( latest.csv latest.json scores_latest.csv scores_latest.json top5_latest.csv top5_latest.md )
  for f in "${files[@]}"; do
    if [[ -f "$src/$f" ]]; then cp -f "$src/$f" "$dst/$f"; fi
  done
}

main() {
  mkdir -p "$DESK" "$ICLOUD" "$HIST" "$IHIST"

  local attempt=1
  while (( attempt <= RETRIES )); do
    log "Zoek laatste succesvolle run (poging $attempt/$RETRIES)…"
    RUN_ID="$(latest_success_run_id || true)"
    if [[ -z "${RUN_ID:-}" ]]; then
      warn "Nog geen succesvolle run gevonden. Wacht $SLEEP_SECS seconden en probeer opnieuw…"
      sleep "$SLEEP_SECS"; ((attempt++)); continue
    fi
    log "Laatste succesvolle run_id: $RUN_ID"

    WORKDIR="$(mktemp -d /tmp/ghart.XXXXXX)"
    STAMP="$(date -u +%Y-%m-%d_%H%MZ)_$RUN_ID"
    OUT="$HIST/$STAMP"; IOUT="$IHIST/$STAMP"
    mkdir -p "$OUT" "$IOUT"

    ART_NAME="$(artifact_name_for_run "$RUN_ID" || true)"
    if [[ -z "${ART_NAME:-}" ]]; then
      warn "Geen artifact gevonden bij run $RUN_ID. Wacht $SLEEP_SECS seconden en probeer opnieuw…"
      rm -rf "$WORKDIR"; sleep "$SLEEP_SECS"; ((attempt++)); continue
    fi
    log "Artifact-naam: $ART_NAME"

    log "Download artifact…"
    download_artifact "$RUN_ID" "$ART_NAME" "$WORKDIR"

    log "Schrijf historie: $OUT"
    cp -a "$WORKDIR"/. "$OUT/"

    log "Update 'latest' op Desktop + iCloud (indien aanwezig)…"
    copy_latest_if_exists "$WORKDIR" "$DESK"
    copy_latest_if_exists "$WORKDIR" "$ICLOUD"

    log "Sync historie naar iCloud…"
    rsync -a "$OUT/." "$IOUT/"

    write_manifest "$OUT"
    rm -rf "$WORKDIR"

    log "✅ Klaar. Historie: $OUT  |  iCloud: $IOUT"
    log "✅ Latest op Desktop: $DESK | iCloud: $ICLOUD"
    return 0
  done

  err "Opgegeven na $RETRIES pogingen. Controleer of de workflow succesvol heeft gedraaid."
  return 1
}

main "$@"
