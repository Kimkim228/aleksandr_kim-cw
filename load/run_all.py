"""Нагрузочное испытание сервиса заявок (второе задание): один запуск — все испытания.

    python load/run_all.py            # всё по порядку
    python load/run_all.py mixed      # только смешанный профиль по ступеням
    python load/run_all.py heavy      # только тяжёлая операция (сводка)
    python load/run_all.py profile    # только профиль py-spy под нагрузкой
    python load/run_all.py tune       # только опыт с настройкой на ступени излома
    python load/run_all.py explain    # только планы запросов

Запускается из корня репозитория в том же виртуальном окружении, что и сервис; читает .env.
Сервис скрипт запускает и останавливает сам (uvicorn, один процесс, как в первом задании).
Перед каждым испытанием рабочая база наполняется заново. Результаты — в load/results/.
"""
import gzip
import json
import math
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOAD = ROOT / "load"
RES = LOAD / "results"
BIN = LOAD / ".bin"
sys.path.insert(0, str(LOAD))
from analyze import analyze  # noqa: E402

K6_VERSION = "v1.3.0"
K6_URL = f"https://github.com/grafana/k6/releases/download/{K6_VERSION}/k6-{K6_VERSION}-linux-amd64.tar.gz"
MIXED_RATES = [1, 2, 4, 8, 16, 32, 64, 128, 256]
MIXED_DURATION = 30
HEAVY_START_RATE = 0.1
HEAVY_MAX_STAGES = 5
HEAVY_DURATION = 90
HEAVY_TIMEOUT = 180
PROFILE_SECONDS = 70
APP_MARK = "app.main:app"
CLK = os.sysconf("SC_CLK_TCK")
PAGE = os.sysconf("SC_PAGE_SIZE")


def say(msg=""):
    print(msg, flush=True)


def load_env():
    env = dict(os.environ)
    p = ROOT / ".env"
    if not p.exists():
        sys.exit("нет файла .env в корне репозитория: cp .env.example .env")
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


ENV = load_env()
DB_URL = ENV["DATABASE_URL"]
APP_PORT = int(ENV.get("APP_PORT", "8029"))
BASE = f"http://127.0.0.1:{APP_PORT}"


def out(cmd, **kw):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT, env=ENV, **kw).stdout.strip()
    except Exception as e:  # noqa: BLE001
        return f"(не выполнено: {e})"


# ---------- состояние прогона ----------

def state():
    p = RES / "state.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def save_state(st):
    (RES / "state.json").write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------- инструменты ----------

def find_k6():
    for c in (shutil.which("k6"), str(BIN / "k6")):
        if c and Path(c).exists():
            return c
    say(f"k6 не найден — скачиваю {K6_VERSION} в load/.bin/ ...")
    BIN.mkdir(exist_ok=True)
    tgz = BIN / "k6.tgz"
    urllib.request.urlretrieve(K6_URL, tgz)
    with tarfile.open(tgz) as t:
        m = next(x for x in t.getmembers() if x.name.endswith("/k6"))
        m.name = "k6"
        t.extract(m, BIN)
    tgz.unlink()
    (BIN / "k6").chmod(0o755)
    return str(BIN / "k6")


def find_pyspy():
    cands = [Path(sys.executable).parent / "py-spy", shutil.which("py-spy")]
    for c in cands:
        if c and Path(c).exists():
            return str(c)
    say("py-spy не найден — устанавливаю в виртуальное окружение ...")
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "py-spy>=0.4.2"], check=True)
    return str(Path(sys.executable).parent / "py-spy")


# ---------- база данных ----------

def db_connect(url=None, **kw):
    import psycopg
    return psycopg.connect(url or DB_URL, connect_timeout=5, **kw)


def ensure_db():
    if not DB_URL.rsplit("/", 1)[-1].startswith("aleksandr_kim_work"):
        sys.exit(f"DATABASE_URL должен указывать на рабочую базу aleksandr_kim_work, сейчас: {DB_URL}")
    subprocess.run(["docker", "compose", "up", "-d", "db"], cwd=ROOT, env=ENV, check=True)
    admin = ENV.get("ADMIN_URL") or DB_URL.rsplit("/", 1)[0] + "/postgres"
    for _ in range(60):
        try:
            with db_connect(admin, autocommit=True) as c:
                name = DB_URL.rsplit("/", 1)[-1].split("?")[0]
                if not c.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone():
                    c.execute(f'CREATE DATABASE "{name}"')
            return
        except Exception as e:  # noqa: BLE001
            err = e
            time.sleep(1)
    sys.exit(f"база данных недоступна: {err}")


def container_id():
    ids = out(["docker", "compose", "ps", "-q", "db"]).splitlines()
    return ids[0].strip() if ids else ""


FRESH = False  # база только что наполнена, и запись в неё ещё не шла


def seed():
    global FRESH
    if FRESH:
        return
    say("наполнение рабочей базы заново ...")
    t = time.time()
    r = subprocess.run([sys.executable, "scripts/seed.py", "work", DB_URL], cwd=ROOT, env=ENV,
                       capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit("наполнение не выполнено:\n" + r.stdout + r.stderr)
    FRESH = True
    say(r.stdout.strip() + f"\n  время наполнения: {time.time() - t:.1f} с")


def counts():
    with db_connect() as c:
        return {t: c.execute(f"SELECT count(*) FROM aleksandr_kim.{t}").fetchone()[0]
                for t in ("users", "categories", "tickets", "comments")}


# ---------- процессы сервиса ----------

def proc_table():
    """pid -> (ppid, comm, cmdline, ticks, rss_pages) по /proc."""
    tab = {}
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            stat = open(f"/proc/{d}/stat").read()
            cmd = open(f"/proc/{d}/cmdline", "rb").read().replace(b"\0", b" ").decode(errors="replace")
            rss = int(open(f"/proc/{d}/statm").read().split()[1])
        except OSError:
            continue
        comm = stat[stat.index("(") + 1:stat.rindex(")")]
        f = stat[stat.rindex(")") + 2:].split()
        tab[int(d)] = (int(f[1]), comm, cmd, int(f[11]) + int(f[12]), rss)
    return tab


def app_pids(tab=None):
    """Процессы сервиса: uvicorn app.main:app и все их потомки (рабочие процессы)."""
    tab = tab or proc_table()
    me = os.getpid()
    roots = {p for p, (pp, comm, cmd, _, _) in tab.items()
             if APP_MARK in cmd and "uvicorn" in cmd and comm != "py-spy" and p != me and "run_all" not in cmd}
    res = set(roots)
    grew = True
    while grew:
        grew = False
        for p, (pp, comm, *_rest) in tab.items():
            if pp in res and p not in res and comm != "py-spy":
                res.add(p)
                grew = True
    return res


def port_busy():
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", APP_PORT)) == 0


def http_json(method, path, body=None, token=None, timeout=30):
    req = urllib.request.Request(BASE + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"null")


class App:
    def __init__(self):
        self.proc = None
        self.log = None
        self.settings = {}

    def command(self, workers):
        cmd = [sys.executable, "-m", "uvicorn", APP_MARK, "--port", str(APP_PORT)]
        if workers:
            cmd += ["--workers", str(workers)]
        return cmd

    def environ(self, pool_max):
        env = dict(ENV)
        if pool_max:
            env["DB_POOL_MAX"] = str(pool_max)
        return env

    def start(self, tag, workers=None, pool_max=None, wrap=None, fatal=True):
        self.stop()
        if port_busy():
            sys.exit(f"порт {APP_PORT} занят: остановите запущенный сервис (Ctrl+C в его окне "
                     f'или pkill -f "uvicorn {APP_MARK}") и повторите запуск')
        self.settings = {"workers": workers or 1, "pool_max": pool_max or 10}
        self.log = open(RES / f"app-{tag}.log", "w", encoding="utf-8")
        cmd = (wrap or []) + self.command(workers)
        self.proc = subprocess.Popen(cmd, cwd=ROOT, env=self.environ(pool_max), stdout=self.log,
                                     stderr=subprocess.STDOUT, start_new_session=True)
        for _ in range(120):
            if self.proc.poll() is not None:
                if not fatal:
                    return False
                sys.exit(f"сервис не запустился, см. load/results/app-{tag}.log")
            if port_busy():
                try:
                    urllib.request.urlopen(BASE + "/", timeout=2).read()
                    return True
                except Exception:  # noqa: BLE001
                    pass
            time.sleep(0.25)
        if not fatal:
            return False
        sys.exit("сервис не ответил за 30 с")

    def warm(self):
        tok = http_json("POST", "/api/login", {"login": "staff1", "password": "demo"})["token"]
        for i in range(5):
            http_json("GET", "/api/tickets?page=1&size=20", token=tok)
            http_json("GET", f"/api/tickets/{i + 1}", token=tok)
            http_json("GET", "/api/categories", token=tok)

    def stop(self):
        if self.proc and self.proc.poll() is None:
            for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(self.proc.pid, sig)
                    self.proc.wait(timeout=5)
                    break
                except Exception:  # noqa: BLE001
                    continue
        self.proc = None
        for _ in range(20):  # потомки (рабочие процессы, сервис под py-spy)
            pids = app_pids()
            if not pids:
                break
            for p in pids:
                try:
                    os.kill(p, signal.SIGKILL)
                except OSError:
                    pass
            time.sleep(0.25)
        if self.log:
            self.log.close()
            self.log = None


# ---------- ряд потребления ресурсов ----------

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_UNITS = {"B": 1 / 1048576, "KIB": 1 / 1024, "MIB": 1, "GIB": 1024, "KB": 1 / 1000, "MB": 1, "GB": 1000}


class Monitor(threading.Thread):
    """Раз в секунду: процессор и память процессов сервиса (/proc), процессор и память контейнера
    базы (docker stats), число соединений с базой — всех, выполняющих запрос и занятых в транзакции."""
    COLS = ["ts", "app_cpu", "app_rss_mb", "app_procs", "db_cpu", "db_mem_mb",
            "conn_total", "conn_active", "conn_idle_tx"]

    def __init__(self, path, cid, interval=1.0):
        super().__init__(daemon=True)
        self.path, self.cid, self.interval = path, cid, interval
        self.latest = {}
        self.db_stat = (None, None, 0.0)
        self.stop_flag = threading.Event()
        self.docker = None

    def docker_reader(self):
        try:
            self.docker = subprocess.Popen(
                ["docker", "stats", "--format", "{{.CPUPerc}};{{.MemUsage}}", self.cid],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            for line in self.docker.stdout:
                line = _ANSI.sub("", line).strip()
                m = re.match(r"([\d.]+)%;([\d.]+)\s*([A-Za-z]+)", line)
                if m:
                    self.db_stat = (float(m.group(1)), float(m.group(2)) * _UNITS.get(m.group(3).upper(), 1),
                                    time.time())
        except Exception:  # noqa: BLE001
            pass

    def conns(self, holder):
        try:
            if holder.get("c") is None or holder["c"].closed:
                holder["c"] = db_connect(autocommit=True)
            return holder["c"].execute(
                "SELECT count(*), count(*) FILTER (WHERE state = 'active'), "
                "count(*) FILTER (WHERE state LIKE 'idle in transaction%%') "
                "FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid()").fetchone()
        except Exception:  # noqa: BLE001
            holder["c"] = None
            return (None, None, None)

    def run(self):
        if self.cid:
            threading.Thread(target=self.docker_reader, daemon=True).start()
        new = not self.path.exists()
        holder, prev, prev_t = {}, {}, time.time()
        with open(self.path, "a", encoding="utf-8") as f:
            if new:
                f.write(",".join(self.COLS) + "\n")
            while not self.stop_flag.wait(self.interval):
                now = time.time()
                tab = proc_table()
                pids = app_pids(tab)
                ticks = {p: tab[p][3] for p in pids}
                delta = sum(t - prev.get(p, t) for p, t in ticks.items())
                cpu = 100.0 * delta / CLK / (now - prev_t)
                rss = sum(tab[p][4] for p in pids) * PAGE / 1048576
                prev, prev_t = ticks, now
                dcpu, dmem, dts = self.db_stat
                if now - dts > 5:
                    dcpu = dmem = None
                ct, ca, ci = self.conns(holder)
                row = dict(zip(self.COLS, [round(now, 2), round(cpu, 1), round(rss, 1), len(pids),
                                           dcpu, None if dmem is None else round(dmem, 1), ct, ca, ci]))
                self.latest = row
                f.write(",".join("" if row[c] is None else str(row[c]) for c in self.COLS) + "\n")
                f.flush()

    def stop(self):
        self.stop_flag.set()
        if self.docker:
            self.docker.terminate()

    def wait_idle(self, limit=150):
        """Ждём возврата к простою: процессор сервиса < 3 % и нет занятых соединений 3 замера подряд."""
        calm, t = 0, time.time()
        while time.time() - t < limit:
            time.sleep(self.interval)
            r = self.latest
            busy = (r.get("conn_active") or 0) + (r.get("conn_idle_tx") or 0)
            calm = calm + 1 if r and r.get("app_cpu", 100) < 3 and busy == 0 else 0
            if calm >= 3:
                return True
        return False


# ---------- запуск k6 ----------

def run_k6(k6, scenario, rate, duration, tag, timeout=120, grace=None, quiet=False):
    raw = RES / f"raw-{tag}.json"
    grace = timeout + 5 if grace is None else grace
    cmd = [k6, "run", "--out", f"json={raw}",
           "-e", f"BASE_URL={BASE}", "-e", f"SCENARIO={scenario}", "-e", f"RATE={rate}",
           "-e", f"DURATION={duration}s", "-e", f"TIMEOUT={timeout}s", "-e", f"GRACEFUL_STOP={grace}s",
           str(LOAD / "test.js")]
    global FRESH
    FRESH = False
    t_start = time.time()
    with open(RES / f"k6-{tag}.txt", "w", encoding="utf-8") as log:
        log.write("$ " + " ".join(cmd[:1] + ["run"] + cmd[4:]).replace(str(ROOT) + "/", "") + "\n")
        p = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        show = False
        for line in p.stdout:
            log.write(line)
            if "THRESHOLDS" in line:
                show = True
            if "NETWORK" in line:
                show = False
            if show and not quiet and line.strip():
                say("    " + line.rstrip())
        code = p.wait()
    t_end = time.time()
    if code not in (0, 99):
        say(f"  k6 завершился с кодом {code}, см. load/results/k6-{tag}.txt")
    res = analyze(str(raw), scenario, duration) if raw.exists() else {"n": 0, "passed": False}
    res.update({"tag": tag, "scenario": scenario, "rate": rate, "duration": duration, "k6_exit": code,
                "t_start": round(t_start, 2), "t_end": round(t_end, 2)})
    if raw.exists():
        with open(raw, "rb") as a, gzip.open(str(raw) + ".gz", "wb") as b:
            shutil.copyfileobj(a, b)
        raw.unlink()
    return res


def fmt(v, nd=1):
    return "—" if v is None else f"{v:.{nd}f}"


def stage_line(r):
    return (f"{r['rate']:>6} з/с | запросов {r['n']:>5} | p50 {fmt(r.get('p50')):>9} | p95 {fmt(r.get('p95')):>9} | "
            f"max {fmt(r.get('max')):>9} мс | ошибок {fmt(r.get('err_pct'), 2):>6} % | пропущено {r.get('dropped', 0):>4} | "
            f"факт. {fmt(r.get('rate_window'), 2):>6} з/с | порог {'выдержан' if r.get('passed') else 'НЕ выдержан'}")


def table(title, rows):
    say("\n" + title)
    for r in rows:
        say("  " + stage_line(r))


# ---------- испытания ----------

def phase_mixed(k6, app, mon):
    say("\n=== Испытание смешанным профилем (ступени по 30 с) ===")
    app.stop()
    seed()
    app.start("mixed")
    app.warm()
    mon.wait_idle(30)
    rows = []
    for rate in MIXED_RATES:
        say(f"\n  ступень {rate} з/с ...")
        r = run_k6(k6, "mixed", rate, MIXED_DURATION, f"mixed-{rate}")
        r["settings"] = dict(app.settings)
        idle = mon.wait_idle()
        r["restarted_after"] = not idle
        if not idle:  # начатые сводки ещё считаются — перезапуск вместо ожидания
            app.start(f"mixed-after-{rate}")
            app.warm()
            mon.wait_idle(30)
        rows.append(r)
        say("  " + stage_line(r))
        st = state()
        st["mixed"] = rows
        save_state(st)
        if not r["passed"] and len(rows) >= 2 and not rows[-2]["passed"]:
            break  # излом и одна ступень за ним — дальше частоту не поднимаем
    st = state()
    st["mixed_counts_after"] = counts()
    save_state(st)
    table("Смешанный профиль:", rows)
    return rows


def phase_heavy(k6, app, mon):
    say("\n=== Испытание тяжёлой операции: GET /api/summary "
        f"(ступени по {HEAVY_DURATION} с: одно выполнение дольше 30 с) ===")
    rows, rate = [], HEAVY_START_RATE
    app.stop()
    seed()
    for i in range(HEAVY_MAX_STAGES):
        app.start(f"heavy-{rate}")
        app.warm()
        mon.wait_idle(30)
        say(f"\n  ступень {rate} з/с ...")
        r = run_k6(k6, "heavy", rate, HEAVY_DURATION, f"heavy-{rate}", timeout=HEAVY_TIMEOUT)
        r["settings"] = dict(app.settings)
        rows.append(r)
        say("  " + stage_line(r) + f" | установившаяся {fmt(r.get('rate_steady'), 3)}, по прогону {fmt(r.get('rate_total'), 3)} з/с")
        st = state()
        st["heavy"] = rows
        save_state(st)
        if i >= 2 and (r.get("rate_total") or 0) < 1.1 * (rows[-2].get("rate_total") or 0):
            break  # фактическая частота перестала расти
        rate = round(rate * 2, 3)
    app.stop()
    table("Тяжёлая операция (сводка):", rows)
    return rows


def heavy_limit():
    rows = state().get("heavy") or []
    vals = [r.get("rate_total") or 0 for r in rows]
    return max(vals) if vals and max(vals) > 0 else 0.2


def phase_profile(k6, app, mon):
    pyspy = find_pyspy()
    rate = min(1.0, max(0.05, round(heavy_limit(), 2)))
    say(f"\n=== Профиль py-spy под нагрузкой сводкой, {rate} з/с, {PROFILE_SECONDS} с ===")
    info = {"rate": rate, "seconds": PROFILE_SECONDS, "pyspy": out([pyspy, "--version"]), "runs": []}
    app.stop()
    seed()
    for fmt_, name in (("raw", "profile.folded"), ("flamegraph", "profile.svg")):
        dst = RES / name
        if dst.exists():
            dst.unlink()
        wrap = [pyspy, "record", "--format", fmt_, "-o", str(dst), "-d", str(PROFILE_SECONDS),
                "--rate", "100", "--"]
        mode = "сервис запущен под py-spy"
        t0 = time.time()
        r = None
        if app.start(f"profile-{fmt_}", wrap=wrap, fatal=False):
            r = run_k6(k6, "heavy", rate, PROFILE_SECONDS - 12, f"profile-{fmt_}", timeout=90, grace=0, quiet=True)
            try:
                app.proc.wait(timeout=PROFILE_SECONDS + 30)
            except Exception:  # noqa: BLE001
                pass
        app.stop()
        if not dst.exists() or dst.stat().st_size == 0:
            # запасной способ: подключение к работающему процессу (может потребовать sudo)
            say("  py-spy в режиме запуска не дал профиля — подключаюсь к процессу по PID ...")
            mode = "py-spy подключён к процессу по PID"
            app.start(f"profile-{fmt_}-pid")
            pid = sorted(app_pids())[0]
            base = [pyspy, "record", "--format", fmt_, "-o", str(dst), "-d", str(PROFILE_SECONDS - 15),
                    "--rate", "100", "--pid", str(pid)]
            rec = subprocess.Popen(base, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            time.sleep(1.5)
            if rec.poll() is not None:
                say("  нужен sudo для py-spy (введите пароль, если спросит):")
                rec = subprocess.Popen(["sudo"] + base, cwd=ROOT)
            r = run_k6(k6, "heavy", rate, PROFILE_SECONDS - 12, f"profile-{fmt_}", timeout=90, grace=0, quiet=True)
            try:
                rec.wait(timeout=PROFILE_SECONDS + 60)
            except Exception:  # noqa: BLE001
                rec.kill()
            app.stop()
        ok = dst.exists() and dst.stat().st_size > 0
        info["runs"].append({"format": fmt_, "file": name, "ok": ok, "mode": mode, "load": r,
                             "elapsed": round(time.time() - t0, 1)})
        say(f"  {name}: {'готов, ' + str(dst.stat().st_size) + ' байт' if ok else 'НЕ ПОЛУЧЕН'}")
    st = state()
    st["profile"] = info
    save_state(st)
    if (RES / "profile.folded").exists():
        top_functions(RES / "profile.folded")


def top_functions(path, n=14):
    """Таблица функций по свёрнутым стекам py-spy: доля снимков, где функция есть в стеке."""
    total, incl, own = 0, {}, {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stack, _, cnt = line.rpartition(" ")
        if not stack or not cnt.isdigit():
            continue
        c = int(cnt)
        total += c
        frames = stack.split(";")
        for fr in set(frames):
            incl[fr] = incl.get(fr, 0) + c
        own[frames[-1]] = own.get(frames[-1], 0) + c
    if not total:
        return
    lines = [f"снимков: {total}", f"{'в стеке, %':>10} {'сама, %':>8}  функция (файл:строка)"]
    mine = [(v, k) for k, v in incl.items() if "(app/" in k or "/app/" in k]
    for v, k in sorted(mine, reverse=True)[:n]:
        lines.append(f"{100 * v / total:>10.1f} {100 * own.get(k, 0) / total:>8.1f}  {k}")
    lines.append("--- самые частые вершины стека (где процесс находился в момент снимка) ---")
    for k, v in sorted(own.items(), key=lambda x: -x[1])[:n]:
        lines.append(f"{'':>10} {100 * v / total:>8.1f}  {k}")
    (RES / "profile-top.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    say("\n".join("  " + x for x in lines))


def phase_tune(k6, app, mon):
    rows = state().get("mixed") or []
    bad = next((r for r in rows if not r["passed"]), None)
    if not bad:
        say("\nопыт с настройкой пропущен: точка излома смешанного профиля не найдена")
        return
    rate = bad["rate"]
    say(f"\n=== Опыт с настройкой: ступень излома {rate} з/с, меняется один параметр ===")
    res = []
    for tag, kw, label in (("workers4", {"workers": 4}, "4 рабочих процесса uvicorn (было 1)"),
                           ("pool40", {"pool_max": 40}, "пул соединений 40 (было 10)")):
        app.stop()
        seed()
        app.start(f"tune-{tag}", **kw)
        app.warm()
        mon.wait_idle(30)
        say(f"\n  {label} ...")
        r = run_k6(k6, "mixed", rate, MIXED_DURATION, f"tune-{tag}")
        r["settings"], r["label"] = dict(app.settings), label
        res.append(r)
        say("  " + stage_line(r))
        st = state()
        st["tune"] = res
        save_state(st)
        if not mon.wait_idle():
            app.stop()
    app.stop()
    say(f"\nОпыт с настройкой (исходная ступень: {stage_line(bad)})")
    for r in res:
        say(f"  {r['label']}:\n  {stage_line(r)}")


EXPLAIN = [
    ("список: подсчёт без отбора", "SELECT count(*) AS n FROM tickets"),
    ("список: подсчёт с отбором status=new", "SELECT count(*) AS n FROM tickets WHERE status = 'new'"),
    ("список: первая страница",
     "SELECT id, title, status, category_id, author_id, created_at FROM tickets "
     "ORDER BY created_at DESC, id DESC LIMIT 20 OFFSET 0"),
    ("список: status=new, первая страница",
     "SELECT id, title, status, category_id, author_id, created_at FROM tickets WHERE status = 'new' "
     "ORDER BY created_at DESC, id DESC LIMIT 20 OFFSET 0"),
    ("список: страница 10",
     "SELECT id, title, status, category_id, author_id, created_at FROM tickets "
     "ORDER BY created_at DESC, id DESC LIMIT 20 OFFSET 180"),
    ("сводка: заявки категории", "SELECT id, status, created_at FROM tickets WHERE category_id = 1"),
    ("сводка: первый ответ сотрудника по одной заявке",
     "SELECT min(c.created_at) AS at FROM comments c JOIN users u ON u.id = c.author_id "
     "WHERE c.ticket_id = 1 AND u.role = 'staff'"),
]


def phase_explain():
    say("\n=== Планы запросов (EXPLAIN ANALYZE) на рабочем наполнении ===")
    lines = []
    with db_connect(autocommit=True) as c:
        c.execute("SET search_path = aleksandr_kim")
        for title, sql in EXPLAIN:
            c.execute(sql).fetchall()  # прогрев
            plan = [r[0] for r in c.execute("EXPLAIN (ANALYZE, BUFFERS) " + sql).fetchall()]
            lines += [f"## {title}", sql, *plan, ""]
            say(f"  {title}: {plan[-1].strip()}")
    (RES / "explain.txt").write_text("\n".join(lines), encoding="utf-8")


def env_info(k6):
    import importlib.metadata as md
    cpu = out(["sh", "-c", "grep -m1 'model name' /proc/cpuinfo | cut -d: -f2"])
    with db_connect() as c:
        pg = c.execute("SELECT version()").fetchone()[0]
    info = {
        "commit": out(["git", "rev-parse", "HEAD"]),
        "commit_short": out(["git", "rev-parse", "--short", "HEAD"]),
        "commit_subject": out(["git", "log", "-1", "--format=%s"]),
        "git_dirty": out(["git", "status", "--porcelain"]),
        "k6": out([k6, "version"]),
        "python": platform.python_version(),
        "packages": {p: md.version(p) for p in ("fastapi", "uvicorn", "psycopg", "psycopg-pool")
                     if _has(md, p)},
        "postgres": pg,
        "docker": out(["docker", "--version"]),
        "cpu": cpu, "logical_cpus": os.cpu_count(),
        "mem": out(["sh", "-c", "free -m | sed -n 2p"]),
        "kernel": platform.release(),
        "os": out(["sh", "-c", ". /etc/os-release; echo $PRETTY_NAME"]),
        "database": DB_URL.rsplit("/", 1)[-1], "account": "staff1",
        "counts_after_seed": counts(),
        "started": time.strftime("%Y-%m-%d %H:%M:%S %z"),
    }
    (RES / "env.json").write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
    say(f"версия кода: {info['commit_short']} ({info['commit_subject']})"
        + ("  — ВНИМАНИЕ: есть незакоммиченные изменения" if info["git_dirty"] else ""))
    say(f"{info['k6']}\nPython {info['python']}, {pg.split(',')[0]}\n{cpu.strip()}, логических процессоров: {os.cpu_count()}")


def _has(md, p):
    try:
        md.version(p)
        return True
    except Exception:  # noqa: BLE001
        return False


def pack():
    z = ROOT / "dz2-results.zip"
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as f:
        for p in sorted(RES.rglob("*")):
            if p.is_file():
                f.write(p, p.relative_to(LOAD))
    say(f"\nрезультаты упакованы: {z} ({z.stat().st_size // 1024} КБ)")
    try:  # WSL: копия в «Загрузки» Windows, чтобы файл было легко приложить
        win = subprocess.run(["cmd.exe", "/c", "echo %USERPROFILE%"], capture_output=True, text=True,
                             timeout=10, cwd="/mnt/c").stdout.strip()
        dst = Path(subprocess.run(["wslpath", win], capture_output=True, text=True).stdout.strip()) / "Downloads"
        if win and dst.is_dir():
            shutil.copy(z, dst / z.name)
            say(f"копия: {win}\\Downloads\\{z.name}")
    except Exception:  # noqa: BLE001
        pass


def main():
    what = sys.argv[1:] or ["all"]
    RES.mkdir(parents=True, exist_ok=True)
    k6 = find_k6()
    ensure_db()
    app, mon = App(), Monitor(RES / "resources.csv", container_id())
    if "all" in what:
        for p in RES.iterdir():  # полный прогон начинается с чистого каталога результатов
            if p.is_file():
                p.unlink()
    mon.start()
    t = time.time()
    try:
        if "all" in what or "explain" in what:
            if "all" in what:
                seed()
            env_info(k6)
            phase_explain()
        if "all" in what or "mixed" in what:
            phase_mixed(k6, app, mon)
        if "all" in what or "heavy" in what:
            phase_heavy(k6, app, mon)
        if "all" in what or "profile" in what:
            phase_profile(k6, app, mon)
        if "all" in what or "tune" in what:
            phase_tune(k6, app, mon)
    finally:
        app.stop()
        mon.stop()
    st = state()
    if st.get("mixed"):
        table("ИТОГ. Смешанный профиль:", st["mixed"])
    if st.get("heavy"):
        table("ИТОГ. Тяжёлая операция (сводка):", st["heavy"])
    if st.get("tune"):
        table("ИТОГ. Опыт с настройкой:", st["tune"])
    say(f"\nвсего времени: {(time.time() - t) / 60:.1f} мин")
    pack()


if __name__ == "__main__":
    main()
