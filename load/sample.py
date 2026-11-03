"""Ряд потребления ресурсов раз в секунду: процессор и память приложения и базы,
соединения с базой (все и выполняющие запрос, pg_stat_activity).
  python load/sample.py <pid приложения> <файл.csv>   (останавливается сигналом TERM)
Процент процессора — от одного ядра, как в docker stats и top."""
import csv, os, signal, subprocess, sys, time
import psycopg

PID, OUT = int(sys.argv[1]), sys.argv[2]
DB = os.environ.get("DB_CONTAINER", "lab-29399-db-1")
URL = os.environ.get("DATABASE_URL")
TICK = os.sysconf("SC_CLK_TCK")
stop = False
signal.signal(signal.SIGTERM, lambda *a: globals().__setitem__("stop", True))


def pids():
    """Процесс сервиса и его дочерние (при --workers N — рабочие процессы)."""
    out = [PID]
    try:
        for t in os.listdir(f"/proc/{PID}/task"):
            out += [int(x) for x in open(f"/proc/{PID}/task/{t}/children").read().split()]
    except OSError:
        pass
    return out


def app_cpu_ticks():
    total = 0
    for p in pids():
        try:
            f = open(f"/proc/{p}/stat").read().rsplit(")", 1)[1].split()
            total += int(f[11]) + int(f[12])   # utime + stime
        except OSError:
            pass
    return total


def app_rss_mb():
    total = 0
    for p in pids():
        try:
            for line in open(f"/proc/{p}/status"):
                if line.startswith("VmRSS"):
                    total += int(line.split()[1]) / 1024
        except OSError:
            pass
    return total


def db_stats():
    out = subprocess.run(["docker", "exec", DB, "sh", "-c",
                          "grep usage_usec /sys/fs/cgroup/cpu.stat; cat /sys/fs/cgroup/memory.current"],
                         capture_output=True, text=True).stdout.split()
    return int(out[1]), int(out[2]) / 2**20   # мкс процессора, МБ памяти


conn = psycopg.connect(URL, autocommit=True)
w = csv.writer(open(OUT, "w", newline=""))
w.writerow(["t", "app_cpu", "app_rss_mb", "db_cpu", "db_mem_mb", "db_conn", "db_active"])
t0 = time.time(); last_t = t0; last_app = app_cpu_ticks(); last_db, _ = db_stats()
while not stop:
    time.sleep(1)
    now = time.time(); app = app_cpu_ticks(); dbu, dbm = db_stats()
    dt = now - last_t
    conns, active = conn.execute(
        "SELECT count(*), count(*) FILTER (WHERE state = 'active') FROM pg_stat_activity "
        "WHERE datname = current_database() AND pid <> pg_backend_pid()").fetchone()
    w.writerow([round(now - t0, 1), round((app - last_app) / TICK / dt * 100, 1), round(app_rss_mb(), 1),
                round((dbu - last_db) / 1e6 / dt * 100, 1), round(dbm, 1), conns, active])
    last_t, last_app, last_db = now, app, dbu
    sys.stdout.flush()
