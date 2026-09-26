#!/usr/bin/env bash
# Host-side checks. Mirrors the firmware's test/check.sh.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== shared constants =="
ARGS=(--check)
[ -n "${INO:-}" ] && ARGS+=(--scan "$INO")
python3 tools/gen_constants.py "${ARGS[@]}"

echo "== tests =="
python3 -m pytest injection_monitor/tests -q

echo "== import check =="
IM_GPIO_ENABLED=0 python3 -c "from injection_monitor import api"
echo "all checks passed"
