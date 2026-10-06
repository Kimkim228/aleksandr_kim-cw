import time

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import DATABASE_URL, SCHEMA

pool = ConnectionPool(
    DATABASE_URL, min_size=2, max_size=10, open=False,
    kwargs={"row_factory": dict_row, "options": f"-c search_path={SCHEMA}"},
)


class Db:
    """Обёртка над соединением: считает число запросов и время, проведённое в базе."""

    def __init__(self, conn):
        self.conn = conn
        self.queries = 0
        self.seconds = 0.0

    def all(self, sql, params=()):
        t = time.perf_counter()
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall() if cur.description else []
        self.seconds += time.perf_counter() - t
        self.queries += 1
        return rows

    def one(self, sql, params=()):
        rows = self.all(sql, params)
        return rows[0] if rows else None

    def commit(self):
        t = time.perf_counter()
        self.conn.commit()
        self.seconds += time.perf_counter() - t


def get_db():
    if pool.closed:
        pool.open()
    with pool.connection() as conn:
        yield Db(conn)
