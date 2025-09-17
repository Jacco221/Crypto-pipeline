#!/usr/bin/env bash
# validate_pipeline.sh — snelle sanity-check op de pipeline-uitvoer
# Werkt op macOS/zsh/bash. Geen extra deps nodig. 'gh' check is optioneel.

set -uo pipefail  # (geen -e: we willen door-checken en alles rapporteren)

# ===== Kleuren/icoontjes =====
bold()  { printf "\033[1m%s\033[0m\n" "$*"; }
ok()    { printf "✅ %s\n" "$*"; }
warn()  { printf "⚠️  %s\n" "$*"; }
fail()  { printf "❌ %s\n" "$*"; }

ANY_FAIL=false

# ===== Helper: file bestaan + minimale grootte (bytes) =====
need_file() {
  local f="$1"; local desc="$2"; local minb="${3:-100}"
  if [[ -f "$f" ]]; then
    local sz
    sz=$(stat -f%z "$f" 2>/dev/null || stat -c%s "$f" 2>/dev/null || echo 0)
    if [[ "$sz" -ge "$minb" ]]; then
      ok "$desc OK  — $(basename "$f") ($sz bytes)"
    else
      warn "$desc gevonden maar erg klein ($sz bytes): $f"
    fi
  else
    fail "$desc ontbreekt: $f"
    ANY_FAIL=true
  fi
}

# ===== Helper: min. aantal regels =====
min_lines() {
  local f="$1"; local want="$2"; local desc="$3"
  if [[ -f "$f" ]]; then
    local n
    n=$(wc -l < "$f" | tr -d ' ')
    if [[ "$n" -ge "$want" ]]; then
      ok "$desc OK  — regels: $n (≥ $want)"
    else
      warn "$desc lijkt leeg/kort — regels: $n (< $want) — $f"
    fi
  else
    # al door need_file afgevangen
    :
  fi
}

# ===== Bepaal REPORTS_DIR =====
REPO_DIR="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
DEFAULT_DESKTOP="$HOME/Desktop/crypto-reports"
DEFAULT_DATA="$REPO_DIR/data/reports"

# volgorde: meegegeven via --reports-dir / $REPORTS_DIR -> data/reports -> Desktop
REPORTS_DIR=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --reports-dir) REPORTS_DIR="$2"; shift 2;;
    *) shift;;
  esac
done
: "${REPORTS_DIR:=${REPORTS_DIR:-}}"
if [[ -z "$REPORTS_DIR" ]]; then
  if [[ -d "$DEFAULT_DATA" ]]; then
    REPORTS_DIR="$DEFAULT_DATA"
  else
    REPORTS_DIR="$DEFAULT_DESKTOP"
  fi
fi

bold "Controleer map: $REPORTS_DIR"
if [[ ! -d "$REPORTS_DIR" ]]; then
  fail "Reports-map bestaat niet: $REPORTS_DIR"
  exit 2
fi

# ===== Verwachte bestanden =====
TOP5_CSV="$REPORTS_DIR/top5_latest.csv"
TOP5_MD="$REPORTS_DIR/top5_latest.md"
SCORES_CSV="$REPORTS_DIR/scores_latest.csv"
SCORES_JSON="$REPORTS_DIR/scores_latest.json"
LATEST_CSV="$REPORTS_DIR/latest.csv"
LATEST_JSON="$REPORTS_DIR/latest.json"

MOON_CSV="$REPORTS_DIR/moonshots_v2_latest.csv"
MOON_MD="$REPORTS_DIR/moonshots_v2_latest.md"

# ===== Basischecks =====
need_file "$TOP5_CSV"   "Top-5 CSV"
min_lines "$TOP5_CSV"  "2" "Top-5 CSV (≥ header + 1 regel)"

need_file "$TOP5_MD"    "Top-5 rapport (MD)" "150"
need_file "$SCORES_CSV" "Scores CSV"         "400"
need_file "$SCORES_JSON""Scores JSON"        "400"
need_file "$LATEST_CSV" "Latest CSV"         "50"
need_file "$LATEST_JSON""Latest JSON"        "50"

# ===== Moonshot (optioneel) =====
if [[ -f "$MOON_CSV" || -f "$MOON_MD" ]]; then
  need_file "$MOON_CSV" "Moonshots v2 CSV" "80"
  min_lines "$MOON_CSV" "2" "Moonshots v2 CSV (≥ header + 1 regel)"
  need_file "$MOON_MD"  "Moonshots v2 rapport (MD)" "120"
else
  warn "Moonshots v2 rapporten niet gevonden (optioneel)."
fi

# ===== Mtime sanity (te snelle of te oude outputs?) =====
# Als alles in < 10s is aangemaakt, is dat vaak verdacht (lege of cached artefacten).
now=$(date +%s)
too_fast=true
for f in "$TOP5_CSV" "$SCORES_CSV" "$LATEST_CSV"; do
  if [[ -f "$f" ]]; then
    mt=$(stat -f%m "$f" 2>/dev/null || stat -c %Y "$f" 2>/dev/null || echo 0)
    age=$(( now - mt ))
    # vers genoeg?
    if [[ "$age" -lt 3600 ]]; then
      # niet *alle* bestanden supersnel na elkaar? (≥ 10s spread)
      # we gebruiken hier gewoon de mtime check — als 1 bestand ouder is, is too_fast al niet meer “true”.
      too_fast=false
    fi
  fi
done

if [[ "$too_fast" == true ]]; then
  warn "Bestanden lijken niet recent geüpdatet (ouder dan ~1 uur). Controleer je cron/Actions."
fi

# Beetje extra: Top-5 CSV zou 6 regels moeten hebben (header + 5 rijen)
if [[ -f "$TOP5_CSV" ]]; then
  rows=$(tail -n +2 "$TOP5_CSV" | wc -l | tr -d ' ')
  if [[ "$rows" -lt 5 ]]; then
    warn "Top-5 CSV heeft geen 5 rijen (gevonden: $rows)."
  fi
fi

# ===== Optioneel: laatste GitHub Actions run samenvatten (geen hard fail) =====
if command -v gh >/dev/null 2>&1; then
  bold "Laatste GitHub Actions run (pipeline.yml):"
  gh run list --workflow="pipeline.yml" -L 1 || warn "Kon gh run list niet ophalen."
else
  warn "gh (GitHub CLI) niet gevonden — sla Actions-check over."
fi

echo
if [[ "$ANY_FAIL" == true ]]; then
  fail "Pijplijncontrole heeft fouten. Zie meldingen hierboven."
  exit 1
else
  ok "Alles ziet er goed uit ✅"
fi

