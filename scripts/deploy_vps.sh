#!/usr/bin/env bash
set -euo pipefail

# Запускать из корня репозитория. Секреты не принимаются аргументами и не печатаются.
command -v docker >/dev/null || { echo "Docker не установлен." >&2; exit 1; }
if [ ! -f .env ]; then
  python3 scripts/bootstrap_env.py
fi
docker compose config >/dev/null
docker compose up --build -d
echo "Готово. Откройте http://IP_ВАШЕГО_VPS:${HTTP_PORT:-8080}"
echo "Проверка: curl http://127.0.0.1:${HTTP_PORT:-8080}/health/"
