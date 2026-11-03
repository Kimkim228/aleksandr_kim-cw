#!/usr/bin/env bash
# Запуск сервиса для испытаний: рабочая база, порт 8030, один процесс (как в ДЗ № 1).
# Журнал доступа и ошибок — load/results/app.log.  WORKERS=4 load/serve.sh — опыт с настройкой.
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a
mkdir -p load/results
pkill -f "uvicorn app.main:app.*--port ${PORT:-8030}" 2>/dev/null; sleep 1
nohup .venv/bin/uvicorn app.main:app --port "${PORT:-8030}" --workers "${WORKERS:-1}" \
  >> load/results/app.log 2>&1 &
until curl -sf -o /dev/null "http://localhost:${PORT:-8030}/"; do sleep 0.5; done
echo "сервис запущен, pid $(pgrep -f "uvicorn app.main:app.*--port ${PORT:-8030}" | head -1)"
