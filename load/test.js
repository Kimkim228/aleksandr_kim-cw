// Нагрузочный тест сервиса заявок (aleksandr_kim), k6.
// Два сценария: mixed — операции по весам профиля нагрузки, heavy — только сводка
// (самая тяжёлая операция по замерам первого задания). Модель нагрузки открытая:
// частота в запросах в секунду (constant-arrival-rate), частота и длительность ступени —
// параметрами запуска:
//   k6 run -e SCENARIO=mixed -e RATE=8 -e DURATION=30s load/test.js
//   k6 run -e SCENARIO=heavy -e RATE=0.2 -e DURATION=90s load/test.js
import http from 'k6/http';
import exec from 'k6/execution';
import { Counter, Trend } from 'k6/metrics';

const BASE = __ENV.BASE_URL || 'http://127.0.0.1:8029';
const SCENARIO = __ENV.SCENARIO || 'mixed';
const RATE = parseFloat(__ENV.RATE || '1');            // запросов в секунду, можно дробное
const DURATION = __ENV.DURATION || '30s';
const TIMEOUT = __ENV.TIMEOUT || '120s';               // предел ожидания одного ответа
const GRACE = __ENV.GRACEFUL_STOP || '120s';           // сколько ждать начатые запросы после ступени
const TICKETS = parseInt(__ENV.TICKETS || '75000', 10); // заявок в рабочем наполнении
const LOGIN = __ENV.LOGIN || 'staff1';
const PASSWORD = __ENV.PASSWORD || 'demo';
const PRE_VUS = parseInt(__ENV.PRE_VUS || String(Math.max(20, Math.ceil(RATE * 2))), 10);
const MAX_VUS = parseInt(__ENV.MAX_VUS || String(Math.min(1000, Math.max(100, Math.ceil(RATE * 20)))), 10);

// Профиль нагрузки: вес — доля операции в потоке, сумма весов 100 (обоснование — в отчёте).
const PROFILE = [
  { op: 'list', weight: 25 },         // GET /api/tickets, первая страница
  { op: 'list_filter', weight: 15 },  // GET /api/tickets с отбором по статусу или категории
  { op: 'list_page', weight: 5 },     // GET /api/tickets, страницы 2–10
  { op: 'card', weight: 28 },         // GET /api/tickets/{id}
  { op: 'categories', weight: 5 },    // GET /api/categories
  { op: 'create', weight: 5 },        // POST /api/tickets
  { op: 'comment', weight: 12 },      // POST /api/tickets/{id}/comments
  { op: 'status', weight: 4 },        // POST /api/tickets/{id}/status
  { op: 'summary', weight: 1 },       // GET /api/summary
];

// Порядок операций — детерминированный: на каждые 100 запросов подряд каждая операция
// встречается ровно столько раз, каков её вес (плавное взвешенное чередование).
const CYCLE = [];
const RANK = [];
(function build() {
  const cur = PROFILE.map(() => 0);
  const seen = PROFILE.map(() => 0);
  for (let i = 0; i < 100; i++) {
    let best = 0;
    for (let j = 0; j < PROFILE.length; j++) {
      cur[j] += PROFILE[j].weight;
      if (cur[j] > cur[best]) best = j;
    }
    cur[best] -= 100;
    CYCLE.push(best);
    RANK.push(seen[best]++);
  }
})();

// Порог один для всех (подраздел 2.1 методички): p95 < 500 мс, ошибок < 1 %.
// Отбор по сценарию исключает из расчёта вход и подготовку в setup().
export const options = {
  scenarios: {
    [SCENARIO]: {
      executor: 'constant-arrival-rate',
      rate: Math.round(RATE * 100),
      timeUnit: '100s',
      duration: DURATION,
      preAllocatedVUs: Math.min(PRE_VUS, MAX_VUS),
      maxVUs: MAX_VUS,
      gracefulStop: GRACE,
      exec: SCENARIO === 'heavy' ? 'heavy' : 'mixed',
    },
  },
  thresholds: {
    [`http_req_duration{scenario:${SCENARIO}}`]: ['p(95)<500'],
    [`http_req_failed{scenario:${SCENARIO}}`]: ['rate<0.01'],
  },
  summaryTrendStats: ['min', 'med', 'p(95)', 'max', 'count'],
  setupTimeout: '180s',
};

const dbTime = new Trend('db_time_ms');       // время в базе по заголовку X-DB-Time-Ms
const dbQueries = new Trend('db_queries');    // число запросов к базе по заголовку X-DB-Queries
const refusals = new Counter('expected_refusals'); // отказы по правилам варианта (409)

function ids(token, status, pages) {
  const out = [];
  for (let p = 1; p <= pages; p++) {
    const r = http.get(`${BASE}/api/tickets?status=${status}&size=100&page=${p}`,
      { headers: { Authorization: `Bearer ${token}` }, timeout: TIMEOUT });
    if (r.status !== 200) throw new Error(`подготовка: список ${status} вернул ${r.status}`);
    for (const t of r.json().items) out.push(t.id);
  }
  return out;
}

// Вход — один раз до нагрузки; в результаты не попадает (нет метки сценария).
export function setup() {
  const r = http.post(`${BASE}/api/login`, JSON.stringify({ login: LOGIN, password: PASSWORD }),
    { headers: { 'Content-Type': 'application/json' } });
  if (r.status !== 200) throw new Error(`вход не выполнен: ${r.status}`);
  const token = r.json().token;
  if (SCENARIO === 'heavy') return { token };
  // незакрытые заявки для комментариев и смены статуса
  return { token, newIds: ids(token, 'new', 5), progIds: ids(token, 'in_progress', 5) };
}

function call(data, op, method, path, body, expected) {
  const res = http.request(method, BASE + path, body ? JSON.stringify(body) : null, {
    headers: { Authorization: `Bearer ${data.token}`, 'Content-Type': 'application/json' },
    tags: { op: op, name: op },
    timeout: TIMEOUT,
    // отказы по правилам варианта (409) — ожидаемые ответы, а не ошибки
    responseCallback: http.expectedStatuses(...expected),
  });
  const t = res.headers['X-Db-Time-Ms'] || res.headers['X-DB-Time-Ms'];
  const q = res.headers['X-Db-Queries'] || res.headers['X-DB-Queries'];
  if (t !== undefined) dbTime.add(parseFloat(t), { op: op });
  if (q !== undefined) dbQueries.add(parseInt(q, 10), { op: op });
  if (res.status === 409) refusals.add(1, { op: op });
  return res;
}

export function mixed(data) {
  const it = exec.scenario.iterationInTest;
  const slot = it % 100;
  const op = PROFILE[CYCLE[slot]].op;
  const k = Math.floor(it / 100) * PROFILE[CYCLE[slot]].weight + RANK[slot]; // номер вызова этой операции
  switch (op) {
    case 'list':
      return call(data, op, 'GET', '/api/tickets?page=1&size=20', null, [200]);
    case 'list_filter':
      return call(data, op, 'GET', k % 3 === 2
        ? `/api/tickets?page=1&size=20&category_id=${(k % 10) + 1}`
        : '/api/tickets?page=1&size=20&status=new', null, [200]);
    case 'list_page':
      return call(data, op, 'GET', `/api/tickets?page=${2 + (k % 9)}&size=20`, null, [200]);
    case 'card':
      return call(data, op, 'GET', `/api/tickets/${1 + (it * 7919) % TICKETS}`, null, [200]);
    case 'categories':
      return call(data, op, 'GET', '/api/categories', null, [200]);
    case 'create':
      return call(data, op, 'POST', '/api/tickets',
        { title: `k6 заявка ${it}`, body: 'нагрузочный тест', category_id: (k % 10) + 1 }, [201]);
    case 'comment': {
      // комментарий к незакрытой заявке; если её успели закрыть — 409 по правилу варианта
      const pool = k % 2 === 0 ? data.newIds : data.progIds;
      const id = pool[Math.floor(k / 2) % pool.length];
      return call(data, op, 'POST', `/api/tickets/${id}/comments`, { body: `k6 ответ ${it}` }, [201, 409]);
    }
    case 'status': {
      // сотрудник берёт новую заявку в работу или закрывает заявку в работе;
      // недопустимый переход — 409 по правилу варианта
      const toWork = k % 2 === 0;
      const pool = toWork ? data.newIds : data.progIds;
      const id = pool[pool.length - 1 - (Math.floor(k / 2) % pool.length)];
      return call(data, op, 'POST', `/api/tickets/${id}/status`,
        { status: toWork ? 'in_progress' : 'closed' }, [200, 409]);
    }
    default:
      return call(data, 'summary', 'GET', '/api/summary', null, [200]);
  }
}

export function heavy(data) {
  return call(data, 'summary', 'GET', '/api/summary', null, [200]);
}
