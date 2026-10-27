#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"

if [[ -f "$BASE_DIR/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "$BASE_DIR/.env"
    set +a
fi

cd "$BASE_DIR"
exec "$@"
