#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
SRC_DIR="$BASE_DIR/src"
RUN_DIR="$BASE_DIR/run"
LOG_DIR="$BASE_DIR/logs"
mkdir -p "$RUN_DIR" "$LOG_DIR"
cd "$SRC_DIR"

WEB_HOST="${WEB_HOST:-127.0.0.1}"
WEB_PORT="${WEB_PORT:-8000}"
SERVICE_NAME="${SERVICE_NAME:-mny-web.service}"
PID_FILE="${PID_FILE:-$RUN_DIR/web.pid}"
LOG_FILE="${LOG_FILE:-$LOG_DIR/web.log}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

HEALTH_HOST="$WEB_HOST"
if [[ "$HEALTH_HOST" == "0.0.0.0" || "$HEALTH_HOST" == "::" ]]; then
    HEALTH_HOST="127.0.0.1"
fi
HEALTH_URL="http://${HEALTH_HOST}:${WEB_PORT}/api/dashboard"

if ! "$PYTHON_BIN" -c 'import fastapi, uvicorn' >/dev/null 2>&1; then
    echo "启动失败：$PYTHON_BIN 未安装 fastapi 或 uvicorn" >&2
    exit 1
fi

health_check() {
    "$PYTHON_BIN" - "$HEALTH_URL" <<'PYCHECK' >/dev/null 2>&1
import sys
import urllib.request

with urllib.request.urlopen(sys.argv[1], timeout=2) as response:
    if response.status != 200:
        raise SystemExit(1)
PYCHECK
}

run_systemctl() {
    if [[ "$(id -u)" -eq 0 ]]; then
        systemctl "$@"
    else
        sudo -n systemctl "$@"
    fi
}

if [[ "$WEB_HOST" == "127.0.0.1" && "$WEB_PORT" == "8000" ]] &&     systemctl cat "$SERVICE_NAME" >/dev/null 2>&1; then
    echo "重启 systemd 服务：$SERVICE_NAME"
    if ! run_systemctl restart "$SERVICE_NAME"; then
        echo "重启失败：需要免密 sudo，或手动执行 sudo systemctl restart $SERVICE_NAME" >&2
        exit 1
    fi

    for _ in {1..30}; do
        if health_check; then
            MAIN_PID="$(systemctl show -p MainPID --value "$SERVICE_NAME" 2>/dev/null || true)"
            echo "前端已重启：http://${HEALTH_HOST}:${WEB_PORT}"
            echo "服务：$SERVICE_NAME"
            echo "PID：${MAIN_PID:-未知}"
            echo "日志：$LOG_FILE"
            echo "状态：$(systemctl is-active "$SERVICE_NAME" 2>/dev/null || true)"
            exit 0
        fi
        sleep 0.5
    done

    echo "前端健康检查超时，最近服务日志：" >&2
    sudo -n journalctl -u "$SERVICE_NAME" -n 30 --no-pager >&2 2>/dev/null ||         tail -n 30 "$LOG_FILE" >&2 || true
    exit 1
fi

PIDS=()
if [[ -f "$PID_FILE" ]]; then
    OLD_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ "$OLD_PID" =~ ^[0-9]+$ ]] && kill -0 "$OLD_PID" 2>/dev/null; then
        OLD_CMD="$(tr '\0' ' ' < "/proc/$OLD_PID/cmdline" 2>/dev/null || true)"
        if [[ "$OLD_CMD" == *"uvicorn"* && "$OLD_CMD" == *"web:app"* ]]; then
            PIDS+=("$OLD_PID")
        fi
    fi
fi

while IFS= read -r pid; do
    [[ -n "$pid" ]] && PIDS+=("$pid")
done < <(pgrep -f "uvicorn web:app.*--port ${WEB_PORT}([^0-9]|$)" 2>/dev/null || true)

if ((${#PIDS[@]} > 0)); then
    mapfile -t PIDS < <(printf '%s\n' "${PIDS[@]}" | sort -u)
    echo "停止旧前端进程：${PIDS[*]}"
    kill "${PIDS[@]}" 2>/dev/null || true
    for _ in {1..50}; do
        RUNNING=0
        for pid in "${PIDS[@]}"; do
            if kill -0 "$pid" 2>/dev/null; then
                RUNNING=1
                break
            fi
        done
        ((RUNNING == 0)) && break
        sleep 0.1
    done
    for pid in "${PIDS[@]}"; do
        if kill -0 "$pid" 2>/dev/null; then
            kill -9 "$pid" 2>/dev/null || true
        fi
    done
fi

rm -f "$PID_FILE"
: > "$LOG_FILE"
nohup "$PYTHON_BIN" -m uvicorn web:app     --host "$WEB_HOST" --port "$WEB_PORT" >> "$LOG_FILE" 2>&1 &
NEW_PID=$!
printf '%s\n' "$NEW_PID" > "$PID_FILE"

for _ in {1..30}; do
    if ! kill -0 "$NEW_PID" 2>/dev/null; then
        echo "前端启动失败，最近日志：" >&2
        tail -n 30 "$LOG_FILE" >&2 || true
        rm -f "$PID_FILE"
        exit 1
    fi
    if health_check; then
        echo "前端已重启：http://${HEALTH_HOST}:${WEB_PORT}"
        echo "PID：$NEW_PID"
        echo "日志：$LOG_FILE"
        exit 0
    fi
    sleep 0.5
done

echo "前端健康检查超时，最近日志：" >&2
tail -n 30 "$LOG_FILE" >&2 || true
kill "$NEW_PID" 2>/dev/null || true
rm -f "$PID_FILE"
exit 1
