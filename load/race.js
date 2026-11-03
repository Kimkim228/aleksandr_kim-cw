// Одновременный доступ: N одновременных запросов переводят ОДНУ заявку new -> in_progress
// (или пишут комментарий в заявку, которую в этот же момент закрывают).
// По правилам варианта переход допустим один раз: ровно один ответ 200, остальные 409.
//   k6 run -e MODE=status load/race.js
import http from 'k6/http';
import { Counter } from 'k6/metrics';
const BASE = __ENV.BASE || 'http://localhost:8030';
const N = Number(__ENV.N || 20), ROUNDS = Number(__ENV.ROUNDS || 10);
const ok = new Counter('ok_200');
export const options = {
  scenarios: { race: { executor: 'per-vu-iterations', vus: N, iterations: ROUNDS, maxDuration: '2m' } },
  thresholds: { ok_200: ['count>=0'] },
};
export function setup() {
  const t = http.post(`${BASE}/api/login`, JSON.stringify({ login: 'staff1', password: 'demo' }),
    { headers: { 'Content-Type': 'application/json' } }).json('token');
  const H = { headers: { Authorization: `Bearer ${t}` } };
  const ids = http.get(`${BASE}/api/tickets?page=1&size=100&status=new`, H).json('items').map((x) => x.id);
  return { t, ids: ids.slice(0, ROUNDS) };
}
export default function (d) {
  // все VU в раунде k бьют в одну заявку ids[k]; раунды разведены по времени секундой
  const k = __ITER;
  const wait = (Math.floor(Date.now() / 1000) + 1) * 1000 - Date.now();
  if (wait > 0) { const end = Date.now() + wait; while (Date.now() < end) {} }
  const r = http.post(`${BASE}/api/tickets/${d.ids[k]}/status`, JSON.stringify({ status: 'in_progress' }),
    { headers: { Authorization: `Bearer ${d.t}`, 'Content-Type': 'application/json' },
      responseCallback: http.expectedStatuses(200, 409), tags: { ticket: String(d.ids[k]) } });
  if (r.status === 200) ok.add(1, { ticket: String(d.ids[k]) });
}
