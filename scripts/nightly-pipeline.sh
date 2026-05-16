#!/usr/bin/env bash
# wa-pay-hub/scripts/nightly-pipeline.sh
# Nightly pipeline for WA Pay Hub. Run via crontab at 03:30 ET.

set -uo pipefail
export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:$PATH"

SCRIPTS_DIR="$HOME/wa-pay-hub/scripts"
LOG_FILE="$SCRIPTS_DIR/pipeline.log"
LOCK_FILE="$SCRIPTS_DIR/.nightly-pipeline.lock"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"; }

if [[ -f "$LOCK_FILE" ]]; then
  old_pid=$(cat "$LOCK_FILE" 2>/dev/null || true)
  if [[ -n "$old_pid" ]] && kill -0 "$old_pid" 2>/dev/null; then
    log "Already running (PID $old_pid). Exiting."
    exit 0
  fi
  rm -f "$LOCK_FILE"
fi
echo $$ > "$LOCK_FILE"
trap "rm -f $LOCK_FILE" EXIT INT TERM

log "=== WA nightly pipeline started ==="

log "--- search-greenhouse.py ---"
python3 "$SCRIPTS_DIR/search-greenhouse.py" >> "$LOG_FILE" 2>&1
log "greenhouse done (exit $?)"

log "--- update-jobs.py ---"
python3 "$SCRIPTS_DIR/update-jobs.py" >> "$LOG_FILE" 2>&1
log "update done (exit $?)"

log "--- publish.sh ---"
bash "$SCRIPTS_DIR/publish.sh" >> "$LOG_FILE" 2>&1
log "publish done (exit $?)"

log "=== WA nightly pipeline complete ==="
