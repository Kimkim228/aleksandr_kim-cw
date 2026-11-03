#!/usr/bin/env bash
# Ступенчатое испытание: перед ступенью база наполняется заново, затем ждём, пока
# процессор приложения и базы вернётся к простою; во время ступени снимается ряд ресурсов.
#   load/steps.sh mixed   "2 4 8 16 32"
#   load/steps.sh summary "1 2"
set -u
cd "$(dirname "$0")/.."
SCEN=$1; RATES=$2; DUR=${DURATION:-30s}
OUT=${OUT:-load/results/$SCEN}; mkdir -p "$OUT"
set -a; . ./.env; set +a
K6=${K6:-k6}; PORT=${PORT:-8030}
PID=$(pgrep -f "uvicorn app.main:app.*--port $PORT" | head -1)
[ -n "$PID" ] || { echo "сервис на порту $PORT не запущен (load/serve.sh)"; exit 1; }

ticks() { for p in $PID $(pgrep -P "$PID"); do awk '{print $14+$15}' /proc/$p/stat 2>/dev/null; done | awk '{s+=$1} END{print s+0}'; }
idle() {   # ждём простоя: приложение < 5 % ядра и нет активных запросов в базе
  for i in $(seq 1 600); do
    a=$(ticks); sleep 2; b=$(ticks)
    act=$(docker exec lab-29399-db-1 psql -U app -d aleksandr_kim_work -Atc \
      "select count(*) from pg_stat_activity where state='active' and pid<>pg_backend_pid()")
    [ $(( b - a )) -lt 10 ] && [ "$act" = "0" ] && return 0
  done
}

for R in $RATES; do
  echo "== $SCEN, $R запр/с: наполнение базы"
  .venv/bin/python scripts/seed.py work "$DATABASE_URL" >/dev/null
  idle
  .venv/bin/python load/sample.py "$PID" "$OUT/res-$R.csv" & SP=$!
  sleep 3
  $K6 run --quiet -e SCENARIO="$SCEN" -e RATE="$R" -e DURATION="$DUR" -e BASE="http://localhost:$PORT" \
      --summary-export "$OUT/k6-$R.json" load/test.js > "$OUT/k6-$R.txt" 2>&1
  echo "   k6 завершён, код $? (99 — порог не выдержан)"
  idle
  kill -TERM $SP; wait $SP 2>/dev/null
done
