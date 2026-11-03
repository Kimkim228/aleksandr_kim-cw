#!/usr/bin/env bash
# Профиль под нагрузкой: сервис запускается под py-spy (ptrace_scope=1 разрешает только
# дочерний процесс), сводка идёт на частоте предела, профиль — DUR секунд.
#   load/profile.sh [частота] [секунд] [cpu|idle]
cd "$(dirname "$0")/.."
RATE=${1:-0.05}; DUR=${2:-60}; MODE=${3:-cpu}
set -a; . ./.env; set +a
export PATH=$HOME/bin:$PATH
OUT=load/results/profile; mkdir -p "$OUT"
pkill -f "uvicorn app.main:app.*--port 8030"; sleep 1
.venv/bin/python scripts/seed.py work "$DATABASE_URL" >/dev/null
EXTRA=""; [ "$MODE" = idle ] && EXTRA="--idle"
.venv/bin/py-spy record -r 100 -d "$DUR" $EXTRA -f raw -o "$OUT/summary-$MODE.txt" -- \
  .venv/bin/uvicorn app.main:app --port 8030 >> load/results/app.log 2>&1 &
until curl -sf -o /dev/null http://localhost:8030/; do sleep 0.3; done
k6 run --quiet -e SCENARIO=summary -e RATE="$RATE" -e DURATION="${DUR}s" -e GRACEFUL=5s \
  --summary-export "$OUT/k6-$MODE.json" load/test.js > "$OUT/k6-$MODE.txt" 2>&1
wait
pkill -f "uvicorn app.main:app.*--port 8030"; sleep 1
load/serve.sh
