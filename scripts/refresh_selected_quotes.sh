#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
RUN_DIR="$BASE_DIR/run"
LOG_DIR="$BASE_DIR/logs"
DB_PATH="${CN_QUOTES_DB:-$BASE_DIR/data/quotes.db}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
LOG_FILE="${SELECTED_QUOTES_LOG:-$LOG_DIR/selected_quotes.log}"

mkdir -p "$RUN_DIR" "$LOG_DIR"

in_cn_trading_session() {
    local now dow minutes
    dow="$(TZ=Asia/Shanghai date +%u)"
    [[ "$dow" -le 5 ]] || return 1
    now="$(TZ=Asia/Shanghai date +%H%M)"
    minutes=$((10#$now))
    (( (minutes >= 930 && minutes < 1130) || (minutes >= 1300 && minutes < 1500) ))
}

in_cn_trading_session || exit 0

exec 9>"$RUN_DIR/refresh_selected_quotes.lock"
flock -n 9 || exit 0

for attempt in 1 2 3 4 5 6; do
    in_cn_trading_session || break
    if ! "$PYTHON_BIN" "$BASE_DIR/src/collector.py" \
        --market CN \
        --db "$DB_PATH" \
        --selected-only >>"$LOG_FILE" 2>&1; then
        printf '%s selected-only refresh failed (attempt %s)\n' \
            "$(TZ=Asia/Shanghai date '+%Y-%m-%dT%H:%M:%S%z')" "$attempt" >>"$LOG_FILE"
    fi
    (( attempt == 6 )) || sleep 10
done
