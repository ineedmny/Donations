#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
RUN_DIR="$BASE_DIR/run"
LOG_DIR="$BASE_DIR/logs"
DB_PATH="${CN_QUOTES_DB:-$BASE_DIR/data/quotes.db}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
LOG_FILE="${COLLECTOR_LOG:-$LOG_DIR/collector.log}"

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

exec 9>"$RUN_DIR/collect_cn_full_market.lock"
flock -n 9 || exit 0

"$PYTHON_BIN" "$BASE_DIR/src/collector.py" \
    --market CN \
    --db "$DB_PATH" >>"$LOG_FILE" 2>&1
