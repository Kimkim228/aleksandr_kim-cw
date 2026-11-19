"""Разбор сырого вывода k6 (--out json): показатели ступени и по операциям.
Отдельный запуск: python load/analyze.py <raw.json[.gz]> <сценарий> <длительность, с>"""
import gzip
import json
import math
import re
import sys
from datetime import datetime

P95_LIMIT_MS = 500.0
ERR_LIMIT_PCT = 1.0
_TIME = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)$")


def ts(s: str) -> float:
    m = _TIME.match(s)
    frac = (m.group(2) or "0")[:6].ljust(6, "0")
    tz = "+00:00" if m.group(3) == "Z" else m.group(3)
    return datetime.fromisoformat(f"{m.group(1)}.{frac}{tz}").timestamp()


def pct(vals, p):
    """Процентиль по методу ближайшего ранга — как в замерах первого задания."""
    if not vals:
        return None
    s = sorted(vals)
    return s[max(0, math.ceil(p / 100 * len(s)) - 1)]


def med(vals):
    return pct(vals, 50)


def _stats(reqs):
    d = [r["ms"] for r in reqs]
    fail = sum(1 for r in reqs if r["failed"])
    st = {}
    for r in reqs:
        st[r["status"]] = st.get(r["status"], 0) + 1
    return {"n": len(reqs), "failed": fail, "err_pct": round(100 * fail / len(reqs), 2) if reqs else 0.0,
            "p50": round(med(d), 2) if d else None, "p95": round(pct(d, 95), 2) if d else None,
            "max": round(max(d), 2) if d else None, "statuses": st}


def analyze(path: str, scenario: str, duration_s: float) -> dict:
    reqs, failed_at, dropped = [], {}, 0
    db_ms, db_q = {}, {}
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as f:
        for line in f:
            if '"Point"' not in line:
                continue
            o = json.loads(line)
            m, d = o["metric"], o["data"]
            tags = d.get("tags") or {}
            if m == "dropped_iterations":
                dropped += int(d["value"])
            if tags.get("scenario") != scenario:
                continue
            if m == "http_req_duration":
                end = ts(d["time"])
                reqs.append({"end": end, "start": end - d["value"] / 1000.0, "ms": d["value"],
                             "op": tags.get("op", "?"), "status": tags.get("status", "0"),
                             "exp": tags.get("expected_response") == "true", "error": tags.get("error", "")})
            elif m == "db_time_ms":
                db_ms.setdefault(tags.get("op", "?"), []).append(d["value"])
            elif m == "db_queries":
                db_q.setdefault(tags.get("op", "?"), []).append(d["value"])
    for r in reqs:
        r["failed"] = not r["exp"]
    out = _stats(reqs)
    out["dropped"] = dropped
    if reqs:
        t0 = min(r["start"] for r in reqs)
        t_last = max(r["end"] for r in reqs)
        ok_ends = sorted(r["end"] for r in reqs if not r["failed"])
        in_window = [e for e in ok_ends if e <= t0 + duration_s]
        out["t0"], out["t_last"] = round(t0, 3), round(t_last, 3)
        # фактическая частота: успешные ответы, полученные за время ступени, на её длительность
        out["rate_window"] = round(len(in_window) / duration_s, 3)
        # то же по всему прогону вместе с доработкой начатых запросов
        out["rate_total"] = round(len(ok_ends) / (t_last - t0), 3) if t_last > t0 else None
        # установившаяся частота: успешные ответы после первого ответа и до конца ступени
        if in_window and t0 + duration_s - in_window[0] > 1:
            out["rate_steady"] = round((len(in_window) - 1) / (t0 + duration_s - in_window[0]), 3)
        else:
            out["rate_steady"] = None
    ops = {}
    for r in reqs:
        ops.setdefault(r["op"], []).append(r)
    out["ops"] = {}
    for op, rs in sorted(ops.items()):
        s = _stats(rs)
        s["db_ms_p50"] = round(med(db_ms.get(op, [])), 2) if db_ms.get(op) else None
        s["db_q_p50"] = int(med(db_q.get(op, []))) if db_q.get(op) else None
        out["ops"][op] = s
    errs = {}
    for r in reqs:
        if r["failed"]:
            key = f'{r["op"]}|{r["status"]}|{r["error"]}'
            errs[key] = errs.get(key, 0) + 1
    out["errors"] = errs
    out["passed"] = bool(reqs) and out["p95"] < P95_LIMIT_MS and out["err_pct"] < ERR_LIMIT_PCT
    return out


if __name__ == "__main__":
    print(json.dumps(analyze(sys.argv[1], sys.argv[2], float(sys.argv[3])), ensure_ascii=False, indent=1))
