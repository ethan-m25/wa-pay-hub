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

log "--- search-lever.py ---"
python3 "$SCRIPTS_DIR/search-lever.py" >> "$LOG_FILE" 2>&1
log "lever done (exit $?)"

log "--- search-workday.py ---"
python3 "$SCRIPTS_DIR/search-workday.py" >> "$LOG_FILE" 2>&1
log "workday done (exit $?)"

log "--- search-ashby.py ---"
python3 "$SCRIPTS_DIR/search-ashby.py" >> "$LOG_FILE" 2>&1
log "ashby done (exit $?)"
log "--- search-amazon.py ---"
python3 "$SCRIPTS_DIR/search-amazon.py" >> "$LOG_FILE" 2>&1
log "amazon done (exit $?)"
log "--- search-successfactors.py ---"
python3 "$SCRIPTS_DIR/search-successfactors.py" >> "$LOG_FILE" 2>&1
log "successfactors done (exit $?)"
log "--- search-google.py ---"
python3 "$SCRIPTS_DIR/search-google.py" >> "$LOG_FILE" 2>&1
log "google done (exit $?)"
log "--- search-browser.py ---"
python3 "$SCRIPTS_DIR/search-browser.py" >> "$LOG_FILE" 2>&1
log "browser done (exit $?)"


log "--- update-jobs.py ---"
python3 "$SCRIPTS_DIR/update-jobs.py" >> "$LOG_FILE" 2>&1
log "update done (exit $?)"
log "--- monitor-employers ---"
python3 "$HOME/shared-scripts/hub_monitor_employers.py" --hub "$(basename $(dirname $SCRIPTS_DIR) | sed 's/-pay-hub//')" >> "$LOG_FILE" 2>&1
log "monitor-employers done (exit $?)"
log "--- normalize-companies ---"
python3 "$HOME/shared-scripts/hub_normalize_companies.py" --hub "$(basename $REPO_DIR 2>/dev/null || basename $(dirname $SCRIPTS_DIR))" >> "$LOG_FILE" 2>&1
log "normalize done (exit $?)"

log "--- healthcheck ---"
python3 "$HOME/shared-scripts/hub_pipeline_healthcheck.py" --hub "wa" >> "$LOG_FILE" 2>&1
log "healthcheck done (exit $?)"
log "--- salary-qa ---"
python3 "$HOME/shared-scripts/hub_salary_qa.py" --hub "wa" --dry-run >> "$LOG_FILE" 2>&1
log "salary-qa done (exit $?)"


log "--- archive-jobs ---"
python3 "$HOME/shared-scripts/hub_archive_jobs.py" --hub "wa" --limit 50 >> "$LOG_FILE" 2>&1
log "archive done (exit $?)"

log "--- publish.sh ---"
bash "$SCRIPTS_DIR/publish.sh" >> "$LOG_FILE" 2>&1
log "publish done (exit $?)"

log "=== WA nightly pipeline complete ==="
