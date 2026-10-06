def test_list_requires_authorization(client):
    assert client.get("/api/tickets").status_code == 401


def test_login_rejects_wrong_password(client):
    r = client.post("/api/login", json={"login": "staff1", "password": "wrong"})
    assert r.status_code == 401


def test_list_returns_requested_page(client, user_headers):
    r = client.get("/api/tickets?page=1&size=20", headers=user_headers)
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) == 20
    assert body["total"] == 300
    assert {"id", "title", "status", "category", "author"} <= set(body["items"][0])


def test_list_filters_by_status_and_validates_it(client, user_headers):
    r = client.get("/api/tickets?status=closed&size=100", headers=user_headers)
    assert r.status_code == 200
    assert all(t["status"] == "closed" for t in r.json()["items"])
    assert client.get("/api/tickets?status=bogus", headers=user_headers).status_code == 422
    assert client.get("/api/tickets?page=0", headers=user_headers).status_code == 422


def test_card_contains_comments_and_reaction(client, user_headers):
    r = client.get("/api/tickets/1", headers=user_headers)
    assert r.status_code == 200
    body = r.json()
    assert {"comments", "reaction_minutes", "norm_violated", "category", "author"} <= set(body)
    assert client.get("/api/tickets/999999", headers=user_headers).status_code == 404


def test_create_ticket(client, user_headers):
    r = client.post("/api/tickets", headers=user_headers, json={"title": "Не работает почта", "body": "x", "category_id": 2})
    assert r.status_code == 201
    assert r.json()["status"] == "new"
    bad = client.post("/api/tickets", headers=user_headers, json={"title": "x", "body": "x", "category_id": 999})
    assert bad.status_code == 404


def test_status_change_requires_staff_and_valid_transition(client, user_headers, staff_headers):
    created = client.post("/api/tickets", headers=user_headers, json={"title": "t", "body": "b", "category_id": 1}).json()
    url = f"/api/tickets/{created['id']}/status"
    assert client.post(url, headers=user_headers, json={"status": "in_progress"}).status_code == 403
    assert client.post(url, headers=staff_headers, json={"status": "closed"}).status_code == 409
    assert client.post(url, headers=staff_headers, json={"status": "in_progress"}).status_code == 200


def test_summary_counts_open_tickets(client, user_headers):
    r = client.get("/api/summary", headers=user_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 300
    assert sum(c["open"] for c in body["open_by_category"]) == body["open_total"]
    assert 0 <= body["violated_share"] <= 1
