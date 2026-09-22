#!/usr/bin/env bash
set -euo pipefail

# Одинаковый запуск на Linux и VPS. Можно передать --host и --port.
cd "$(dirname "$0")/.."
exec python3 scripts/start.py "$@"
