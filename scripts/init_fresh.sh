#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"

cd "$BASE_DIR"

existing=()
for path in data/quotes.db data/us_quotes.db; do
    if [[ -e "$path" ]]; then
        existing+=("$path")
    fi
done
if ((${#existing[@]} > 0)); then
    echo "检测到已有数据库：${existing[*]}" >&2
    echo "为避免误删，本脚本不会自动覆盖。请先备份并移走这些文件。" >&2
    exit 1
fi

mkdir -p data logs debug run backups
"$PYTHON_BIN" src/trader.py --market CN --init-db
"$PYTHON_BIN" src/trader.py --market US --init-db

echo
echo "初始化完成：已创建空的 A 股和美股数据库。"
echo "下一步：python3 src/collector.py --market CN"
