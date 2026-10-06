import os
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from . import rules
from .auth import current_user, hash_password, make_token
from .db import Db, get_db

app = FastAPI(title="Сервис учёта заявок")
STATIC = os.path.join(os.path.dirname(__file__), "static")


def reply(db: Db, data, status=200):
    """Ответ с заголовками для измерений: время в базе и число запросов."""
    headers = {"X-DB-Time-Ms": f"{db.seconds * 1000:.3f}", "X-DB-Queries": str(db.queries)}
    return JSONResponse(content=data, status_code=status, headers=headers)


def jsonable(row: dict) -> dict:
    return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in row.items()}


class LoginIn(BaseModel):
    login: str
    password: str


class TicketIn(BaseModel):
    title: str
    body: str
    category_id: int


class CommentIn(BaseModel):
    body: str


class StatusIn(BaseModel):
    status: str


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))


@app.post("/api/login")
def login(data: LoginIn, db: Db = Depends(get_db)):
    user = db.one("SELECT id, role, salt, password_hash FROM users WHERE login = %s", (data.login,))
    if user is None or hash_password(data.password, user["salt"]) != user["password_hash"]:
        raise HTTPException(status_code=401, detail="bad credentials")
    return reply(db, {"token": make_token(user["id"], user["role"]), "role": user["role"]})


@app.get("/api/categories")
def categories(user=Depends(current_user), db: Db = Depends(get_db)):
    return reply(db, db.all("SELECT id, name, reaction_norm_minutes FROM categories ORDER BY id"))


@app.get("/api/tickets")
def list_tickets(page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100),
                 status: str | None = None, category_id: int | None = None,
                 user=Depends(current_user), db: Db = Depends(get_db)):
    if status is not None and status not in rules.STATUSES:
        raise HTTPException(status_code=422, detail="unknown status")
    where, params = [], []
    if status:
        where.append("status = %s")
        params.append(status)
    if category_id:
        where.append("category_id = %s")
        params.append(category_id)
    cond = ("WHERE " + " AND ".join(where)) if where else ""
    total = db.one(f"SELECT count(*) AS n FROM tickets {cond}", params)["n"]
    rows = db.all(
        f"SELECT id, title, status, category_id, author_id, created_at FROM tickets {cond} "
        f"ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s", params + [size, (page - 1) * size])
    items = []
    for r in rows:
        cat = db.one("SELECT name FROM categories WHERE id = %s", (r["category_id"],))
        author = db.one("SELECT name FROM users WHERE id = %s", (r["author_id"],))
        r["category"] = cat["name"]
        r["author"] = author["name"]
        items.append(jsonable(r))
    return reply(db, {"items": items, "total": total, "page": page, "size": size})


@app.get("/api/tickets/{ticket_id}")
def ticket_card(ticket_id: int, user=Depends(current_user), db: Db = Depends(get_db)):
    t = db.one("SELECT * FROM tickets WHERE id = %s", (ticket_id,))
    if t is None:
        raise HTTPException(status_code=404, detail="ticket not found")
    cat = db.one("SELECT name, reaction_norm_minutes FROM categories WHERE id = %s", (t["category_id"],))
    author = db.one("SELECT name FROM users WHERE id = %s", (t["author_id"],))
    comments = db.all(
        "SELECT c.id, c.body, c.created_at, c.author_id, u.name AS author, u.role "
        "FROM comments c JOIN users u ON u.id = c.author_id WHERE c.ticket_id = %s ORDER BY c.created_at, c.id",
        (ticket_id,))
    first_staff = next((c["created_at"] for c in comments if c["role"] == "staff"), None)
    now = datetime.now(timezone.utc)
    t["category"] = cat["name"]
    t["author"] = author["name"]
    t["reaction_minutes"] = rules.reaction_minutes(t["created_at"], first_staff)
    t["norm_violated"] = rules.norm_violated(t["created_at"], first_staff, cat["reaction_norm_minutes"], now)
    t["comments"] = [jsonable(c) for c in comments]
    return reply(db, jsonable(t))


@app.post("/api/tickets")
def create_ticket(data: TicketIn, user=Depends(current_user), db: Db = Depends(get_db)):
    if not data.title.strip():
        raise HTTPException(status_code=422, detail="title is empty")
    if db.one("SELECT 1 AS x FROM categories WHERE id = %s", (data.category_id,)) is None:
        raise HTTPException(status_code=404, detail="category not found")
    t = db.one(
        "INSERT INTO tickets (title, body, category_id, author_id, status, created_at) "
        "VALUES (%s, %s, %s, %s, 'new', now()) RETURNING id, title, status, category_id, author_id, created_at",
        (data.title, data.body, data.category_id, user["id"]))
    db.commit()
    return reply(db, jsonable(t), 201)


@app.post("/api/tickets/{ticket_id}/comments")
def add_comment(ticket_id: int, data: CommentIn, user=Depends(current_user), db: Db = Depends(get_db)):
    t = db.one("SELECT id, status FROM tickets WHERE id = %s", (ticket_id,))
    if t is None:
        raise HTTPException(status_code=404, detail="ticket not found")
    if t["status"] == "closed":
        raise HTTPException(status_code=409, detail="ticket is closed")
    if not data.body.strip():
        raise HTTPException(status_code=422, detail="body is empty")
    c = db.one(
        "INSERT INTO comments (ticket_id, author_id, body, created_at) VALUES (%s, %s, %s, now()) "
        "RETURNING id, ticket_id, author_id, body, created_at", (ticket_id, user["id"], data.body))
    db.commit()
    return reply(db, jsonable(c), 201)


@app.post("/api/tickets/{ticket_id}/status")
def change_status(ticket_id: int, data: StatusIn, user=Depends(current_user), db: Db = Depends(get_db)):
    if data.status not in rules.STATUSES:
        raise HTTPException(status_code=422, detail="unknown status")
    if user["role"] != "staff":
        raise HTTPException(status_code=403, detail="staff only")
    t = db.one("SELECT id, status FROM tickets WHERE id = %s", (ticket_id,))
    if t is None:
        raise HTTPException(status_code=404, detail="ticket not found")
    if not rules.can_transition(t["status"], data.status):
        raise HTTPException(status_code=409, detail=f"cannot go from {t['status']} to {data.status}")
    closed = "now()" if data.status == "closed" else "NULL"
    row = db.one(f"UPDATE tickets SET status = %s, closed_at = {closed} WHERE id = %s "
                 "RETURNING id, status, closed_at", (data.status, ticket_id))
    db.commit()
    return reply(db, jsonable(row))


@app.get("/api/summary")
def summary(user=Depends(current_user), db: Db = Depends(get_db)):
    now = datetime.now(timezone.utc)
    result, open_total, violated, total = [], 0, 0, 0
    for cat in db.all("SELECT id, name, reaction_norm_minutes FROM categories ORDER BY id"):
        tickets = db.all("SELECT id, status, created_at FROM tickets WHERE category_id = %s", (cat["id"],))
        open_n = 0
        for t in tickets:
            first = db.one(
                "SELECT min(c.created_at) AS at FROM comments c JOIN users u ON u.id = c.author_id "
                "WHERE c.ticket_id = %s AND u.role = 'staff'", (t["id"],))
            total += 1
            if rules.norm_violated(t["created_at"], first["at"], cat["reaction_norm_minutes"], now):
                violated += 1
            if t["status"] != "closed":
                open_n += 1
        open_total += open_n
        result.append({"category": cat["name"], "open": open_n})
    share = round(violated / total, 4) if total else 0.0
    return reply(db, {"open_by_category": result, "open_total": open_total,
                      "violated": violated, "total": total, "violated_share": share})
