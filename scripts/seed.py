"""Воспроизводимое наполнение: python scripts/seed.py small|work [DATABASE_URL].
Генератор случайных чисел с фиксированным зерном: повторный запуск даёт те же данные."""
import os
import random
import sys
from datetime import datetime, timedelta, timezone

import psycopg

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.auth import hash_password  # noqa: E402

SIZES = {
    "small": dict(users=50, tickets=300, comments=900),
    "work": dict(users=3000, tickets=75000, comments=225000),
}
CATEGORIES = [("Доступ и пароли", 60), ("Почта", 120), ("Сеть", 90), ("Принтеры", 480), ("Оборудование", 240),
              ("Программное обеспечение", 180), ("Телефония", 240), ("Учётные записи", 60),
              ("Резервное копирование", 720), ("Прочее", 480)]
BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)
SPAN_MIN = 60 * 24 * 270


def populate(conn, size: str, schema_path: str):
    cfg = SIZES[size]
    rnd = random.Random(42)
    ncat = 5 if size == "small" else 10
    n_staff = max(5, cfg["users"] // 20)
    with conn.cursor() as cur:
        cur.execute(open(schema_path, encoding="utf-8").read())
        cur.execute("SET search_path = aleksandr_kim")
        with cur.copy("COPY users (login, name, role, salt, password_hash) FROM STDIN") as cp:
            for i in range(1, cfg["users"] + 1):
                role = "staff" if i <= n_staff else "user"
                salt = f"s{i}"
                cp.write_row((f"{role}{i}", f"Сотрудник {i}" if role == "staff" else f"Пользователь {i}",
                              role, salt, hash_password("demo", salt)))
        with cur.copy("COPY categories (name, reaction_norm_minutes) FROM STDIN") as cp:
            for name, norm in CATEGORIES[:ncat]:
                cp.write_row((name, norm))
        created_list = []
        with cur.copy("COPY tickets (title, body, category_id, author_id, status, created_at, closed_at) "
                      "FROM STDIN") as cp:
            for i in range(1, cfg["tickets"] + 1):
                created = BASE + timedelta(minutes=rnd.randrange(SPAN_MIN))
                status = rnd.choices(["new", "in_progress", "closed"], [2, 3, 5])[0]
                closed = created + timedelta(minutes=rnd.randrange(60, 4000)) if status == "closed" else None
                created_list.append(created)
                cp.write_row((f"Заявка {i}", f"Описание проблемы {i}", rnd.randint(1, ncat),
                              rnd.randint(n_staff + 1, cfg["users"]), status, created, closed))
        with cur.copy("COPY comments (ticket_id, author_id, body, created_at) FROM STDIN") as cp:
            for i in range(1, cfg["comments"] + 1):
                tid = rnd.randint(1, cfg["tickets"])
                staff = rnd.random() < 0.5
                author = rnd.randint(1, n_staff) if staff else rnd.randint(n_staff + 1, cfg["users"])
                at = created_list[tid - 1] + timedelta(minutes=rnd.randrange(5, 1500))
                cp.write_row((tid, author, f"Комментарий {i}", at))
        for t in ("users", "categories", "tickets", "comments"):
            cur.execute(f"SELECT setval(pg_get_serial_sequence('{t}', 'id'), (SELECT max(id) FROM {t}))")
        cur.execute("ANALYZE")
    conn.commit()


def counts(conn):
    out = {}
    with conn.cursor() as cur:
        for t in ("users", "categories", "tickets", "comments"):
            cur.execute(f"SELECT count(*) FROM aleksandr_kim.{t}")
            out[t] = cur.fetchone()[0]
    return out


if __name__ == "__main__":
    size = sys.argv[1]
    url = sys.argv[2] if len(sys.argv) > 2 else os.environ["DATABASE_URL"]
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")
    with psycopg.connect(url) as conn:
        populate(conn, size, path)
        print(f"наполнение '{size}' выполнено:")
        for k, v in counts(conn).items():
            print(f"  {k}: {v}")
