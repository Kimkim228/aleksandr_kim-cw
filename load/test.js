// Нагрузочный тест сервиса заявок (aleksandr_kim), ДЗ № 2.
// Открытая модель: constant-arrival-rate, частота в запросах в секунду.
//   k6 run -e SCENARIO=mixed   -e RATE=8 -e DURATION=30s load/test.js   # смешанный профиль
//   k6 run -e SCENARIO=summary -e RATE=1 -e DURATION=30s load/test.js   # только сводка (самая тяжёлая)
import http from 'k6/http';
import { check, fail } from 'k6';
import { Counter } from 'k6/metrics';

const BASE = __ENV.BASE || 'http://localhost:8030';
const SCENARIO = __ENV.SCENARIO || 'mixed';
const RATE = Number(__ENV.RATE || 2);       // запросов в секунду; дробная — реже одного в секунду
// дробная частота (0.05 = один запрос в 20 с) задаётся через timeUnit: k6 принимает только целую rate
const ARRIVAL = RATE >= 1 ? { rate: RATE, timeUnit: '1s' } : { rate: 1, timeUnit: `${Math.round(1 / RATE)}s` };
const DURATION = __ENV.DURATION || '30s';
const LOGIN = __ENV.LOGIN || 'staff1';          // та же учётная запись, что в замерах ДЗ № 1
const PASSWORD = __ENV.PASSWORD || 'demo';
const MAX_VUS = Number(__ENV.MAX_VUS || 200);
const N_TICKETS = 75000;                         // рабочее наполнение
const N_CATEGORIES = 10;

// Профиль нагрузки: вес — доля операции в общем потоке, сумма 100 (обоснование — в README).
export const PROFILE = [
  ['list', 30],          // список заявок, стр. 1
  ['card', 25],          // карточка заявки
  ['list_filter', 15],   // список с отбором по статусу
  ['comment', 13],       // комментарий к заявке
  ['create', 6],         // новая заявка
  ['categories', 5],     // справочник категорий
  ['status', 5],         // смена статуса (сотрудник)
  ['summary', 1],        // сводка
];

const OPS = PROFILE.map(([op]) => op);
// коды ответов по операциям — для разбора ошибок (0 — нет ответа: истёк тайм-аут клиента)
const codes = new Counter('codes');
const CODES = [0, 200, 201, 409, 500];
const exec = SCENARIO === 'mixed' ? 'mixed' : 'heavy';

// Порог для всех один (подраздел 2.1): p95 < 500 мс, ошибок < 1 %.
const thresholds = {
  [`http_req_duration{scenario:${SCENARIO}}`]: ['p(95)<500'],
  [`http_req_failed{scenario:${SCENARIO}}`]: ['rate<0.01'],
  [`http_reqs{scenario:${SCENARIO}}`]: ['count>=0'],
};
// разбивка по операциям для разбора (условие всегда истинно, нужно только для отчёта)
for (const op of OPS) {
  thresholds[`http_req_duration{op:${op}}`] = ['max>=0'];
  thresholds[`http_req_failed{op:${op}}`] = ['rate>=0'];
  for (const c of CODES) thresholds[`codes{op:${op},code:${c}}`] = ['count>=0'];
}

export const options = {
  scenarios: {
    [SCENARIO]: {
      executor: 'constant-arrival-rate',
      rate: ARRIVAL.rate,
      timeUnit: ARRIVAL.timeUnit,
      duration: DURATION,
      preAllocatedVUs: Math.min(MAX_VUS, Math.max(10, Math.ceil(RATE * 2))),
      maxVUs: MAX_VUS,
      // ждём каждый начатый запрос до его тайм-аута (сводка — 180 с), иначе k6 прервёт его
      // молча: такой запрос не попадёт ни в ответы, ни в ошибки
      gracefulStop: __ENV.GRACEFUL || '200s',
      exec,
    },
  },
  thresholds,
  summaryTrendStats: ['min', 'med', 'avg', 'p(90)', 'p(95)', 'max'],
};

// Отказы по правилам варианта — не ошибки: комментарий к закрытой заявке и
// недопустимый переход статуса отвечают 409 (заявку уже перевёл другой запрос).
const RULE_REFUSALS = http.expectedStatuses(200, 201, 409);

function ids(token, status, pages) {
  const out = [];
  for (let p = 1; p <= pages; p++) {
    const r = http.get(`${BASE}/api/tickets?page=${p}&size=100&status=${status}`,
      { headers: { Authorization: `Bearer ${token}` }, tags: { op: 'setup' } });
    if (r.status !== 200) fail(`setup: список ${status} вернул ${r.status}`);
    for (const t of r.json('items')) out.push(t.id);
  }
  return out;
}

// Вход один раз до нагрузки: в результаты ступени он не попадает (у setup нет тега scenario).
export function setup() {
  const r = http.post(`${BASE}/api/login`, JSON.stringify({ login: LOGIN, password: PASSWORD }),
    { headers: { 'Content-Type': 'application/json' }, tags: { op: 'setup' } });
  if (r.status !== 200) fail(`вход не выполнен: ${r.status}`);
  const token = r.json('token');
  return { token, newIds: ids(token, 'new', 5), progIds: ids(token, 'in_progress', 5) };
}

const rnd = (n) => 1 + Math.floor(Math.random() * n);
const pick = (a) => a[Math.floor(Math.random() * a.length)];
const TOTAL_WEIGHT = PROFILE.reduce((s, [, w]) => s + w, 0);

function chooseOp() {
  let x = Math.random() * TOTAL_WEIGHT;
  for (const [op, w] of PROFILE) { if ((x -= w) < 0) return op; }
  return PROFILE[0][0];
}

function call(op, d) {
  const H = { Authorization: `Bearer ${d.token}`, 'Content-Type': 'application/json' };
  const p = (name, extra = {}) => Object.assign({ headers: H, tags: { op, name } }, extra);
  switch (op) {
    case 'list':
      return http.get(`${BASE}/api/tickets?page=1&size=20`, p('GET /api/tickets'));
    case 'list_filter':
      return http.get(`${BASE}/api/tickets?page=1&size=20&status=${pick(['new', 'in_progress'])}`,
        p('GET /api/tickets?status'));
    case 'card':
      return http.get(`${BASE}/api/tickets/${rnd(N_TICKETS)}`, p('GET /api/tickets/{id}'));
    case 'categories':
      return http.get(`${BASE}/api/categories`, p('GET /api/categories'));
    case 'create':
      return http.post(`${BASE}/api/tickets`,
        JSON.stringify({ title: 'load', body: 'нагрузочный тест', category_id: rnd(N_CATEGORIES) }),
        p('POST /api/tickets'));
    case 'comment':
      return http.post(`${BASE}/api/tickets/${pick(Math.random() < 0.5 ? d.newIds : d.progIds)}/comments`,
        JSON.stringify({ body: 'нагрузочный тест' }),
        p('POST /api/tickets/{id}/comments', { responseCallback: RULE_REFUSALS }));
    case 'status': {
      // сотрудник берёт новую заявку в работу или закрывает взятую
      const take = Math.random() < 0.5;
      const id = take ? pick(d.newIds) : pick(d.progIds);
      return http.post(`${BASE}/api/tickets/${id}/status`,
        JSON.stringify({ status: take ? 'in_progress' : 'closed' }),
        p('POST /api/tickets/{id}/status', { responseCallback: RULE_REFUSALS }));
    }
    case 'summary':
      return http.get(`${BASE}/api/summary`, p('GET /api/summary', { timeout: '180s' }));
  }
}

export function mixed(d) {
  const op = chooseOp();
  const r = call(op, d);
  codes.add(1, { op, code: String(r.status) });
  check(r, { 'ожидаемый код': (x) => [200, 201, 409].includes(x.status) });
}

export function heavy(d) {
  const r = call('summary', d);
  codes.add(1, { op: 'summary', code: String(r.status) });
  check(r, { 'сводка 200': (x) => x.status === 200 });
}
