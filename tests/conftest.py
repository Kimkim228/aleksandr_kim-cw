"""Тесты работают на отдельной базе aleksandr_kim_test: она создаётся и наполняется малым объёмом сама."""
import os
import sys

import psycopg
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

ADMIN_URL = os.environ.get("ADMIN_URL", "postgresql://app:changeme@localhost:5439/postgres")
TEST_DB = "aleksandr_kim_test"
TEST_URL = ADMIN_URL.rsplit("/", 1)[0] + "/" + TEST_DB
os.environ["DATABASE_URL"] = TEST_URL


@pytest.fixture(scope="session", autouse=True)
def test_database():
    with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
        exists = admin.execute("SELECT 1 FROM pg_database WHERE datname = %s", (TEST_DB,)).fetchone()
        if not exists:
            admin.execute(f"CREATE DATABASE {TEST_DB}")
    yield


@pytest.fixture()
def fresh_db(test_database):
    import seed
    with psycopg.connect(TEST_URL) as conn:
        seed.populate(conn, "small", os.path.join(ROOT, "scripts", "schema.sql"))
    yield TEST_URL


@pytest.fixture()
def client(fresh_db):
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        yield c


def _login(client, login):
    r = client.post("/api/login", json={"login": login, "password": "demo"})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture()
def user_headers(client):
    return _login(client, "user10")


@pytest.fixture()
def staff_headers(client):
    return _login(client, "staff1")
