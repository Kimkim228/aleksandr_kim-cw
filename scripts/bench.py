"""Замеры времени ответа.
Полный прогон:      python scripts/bench.py <base_url> <метка> [повторов]
Только новые опции: python scripts/bench.py <base_url> <метка> [повторов] extra
  (вход и смена статуса; уже снятые замеры остальных операций не повторяются)
Прогрев (в расчёт не входит), затем серия повторов; учётная запись staff1."""
import json
import statistics
import sys
import time

import httpx

base, label = sys.argv[1], sys.argv[2]
N = int(sys.argv[3]) if len(sys.argv) > 3 else 30
EXTRA_ONLY = len(sys.argv) > 4 and sys.argv[4] == "extra"
WARM = 5

client = httpx.Client(base_url=base, timeout=300)
tok = client.post("/api/login", json={"login": "staff1", "password": "demo"}).json()["token"]
H = {"Authorization": f"Bearer {tok}"}
state = {"n": 0, "s": 0}
# заявки, существующие на обоих объёмах (id <= 300); для комментариев — только не закрытые
OPEN_IDS = [i for i in range(1, 301)
            if client.get(f"/api/tickets/{i}", headers=H).json()["status"] != "closed"]
NEW_IDS = []


def card_id():
    state["n"] += 1
    return 1 + (state["n"] * 7) % 300


def open_id():
    state["n"] += 1
    return OPEN_IDS[state["n"] % len(OPEN_IDS)]


def bench_new_ids():
    """Заявки 'bench' в статусе «новая», созданные замером POST /api/tickets (самые свежие по created_at)."""
    if not NEW_IDS:
        items = client.get("/api/tickets?page=1&size=100&status=new", headers=H).json()["items"]
        NEW_IDS.extend(t["id"] for t in items if t["title"] == "bench")
        if len(NEW_IDS) < WARM + N:
            sys.exit(f"нужно {WARM + N} заявок 'bench' в статусе new, найдено {len(NEW_IDS)}: "
                     "сначала выполните замер POST /api/tickets")
    return NEW_IDS


def change_status():
    """Переход new -> in_progress: у каждой заявки он допустим ровно один раз."""
    ids = bench_new_ids()
    tid = ids[state["s"]]
    state["s"] += 1
    return client.post(f"/api/tickets/{tid}/status", headers=H, json={"status": "in_progress"})


OPS_MAIN = [
    ("GET /api/categories", lambda: client.get("/api/categories", headers=H)),
    ("GET /api/tickets (стр. 1)", lambda: client.get("/api/tickets?page=1&size=20", headers=H)),
    ("GET /api/tickets (status=new)", lambda: client.get("/api/tickets?page=1&size=20&status=new", headers=H)),
    ("GET /api/tickets (стр. 10)", lambda: client.get("/api/tickets?page=10&size=20", headers=H)),
    ("GET /api/tickets/{id}", lambda: client.get(f"/api/tickets/{card_id()}", headers=H)),
    ("GET /api/summary", lambda: client.get("/api/summary", headers=H)),
    ("POST /api/tickets", lambda: client.post("/api/tickets", headers=H,
                                              json={"title": "bench", "body": "bench", "category_id": 1})),
    ("POST /api/tickets/{id}/comments", lambda: client.post(f"/api/tickets/{open_id()}/comments", headers=H,
                                                           json={"body": "bench"})),
]
# операции, добавленные к первой серии замеров: должны идти после POST /api/tickets
OPS_EXTRA = [
    ("POST /api/login", lambda: client.post("/api/login", json={"login": "staff1", "password": "demo"})),
    ("POST /api/tickets/{id}/status", change_status),
]
OPS = OPS_EXTRA if EXTRA_ONLY else OPS_MAIN + OPS_EXTRA


def pct(vals, p):
    s = sorted(vals)
    k = max(0, min(len(s) - 1, int(round(p / 100 * len(s) + 0.5)) - 1))
    return s[k]


rows = []
for name, call in OPS:
    state["n"] = 0
    for _ in range(WARM):
        call()
    state["n"] = 0
    times, dbt, dbq, codes = [], [], [], set()
    for _ in range(N):
        t = time.perf_counter()
        r = call()
        times.append((time.perf_counter() - t) * 1000)
        codes.add(r.status_code)
        dbt.append(float(r.headers.get("X-DB-Time-Ms", 0)))
        dbq.append(int(r.headers.get("X-DB-Queries", 0)))
    rows.append({"op": name, "n": N, "p50": round(statistics.median(times), 2), "p95": round(pct(times, 95), 2),
                 "max": round(max(times), 2), "db_ms": round(statistics.median(dbt), 2),
                 "queries": int(statistics.median(dbq)), "codes": sorted(codes)})
    r_ = rows[-1]
    print(f"{name:32s} p50={r_['p50']:9.2f} p95={r_['p95']:9.2f} max={r_['max']:9.2f} "
          f"db={r_['db_ms']:9.2f} q={r_['queries']:6d} codes={r_['codes']}")
out = f"bench-{label}-extra.json" if EXTRA_ONLY else f"bench-{label}.json"
json.dump(rows, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
