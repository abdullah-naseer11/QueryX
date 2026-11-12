"""Tests the API on a scratch database. The AI and the Docker sandbox are replaced by fakes."""
import json
import uuid

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from app.api import deps
from app.api.main import app
from app.config import API_KEY, DATABASE_URL
from app.llm.client import LLMClient
from app.migrations import lifecycle, store
from app.sandbox.runner import SandboxReport

H = {"X-API-Key": API_KEY}
ADD_NICKNAME = {"summary": "Add nickname", "up_sql": ["SET lock_timeout = '3s';", "ALTER TABLE users ADD COLUMN nickname text;"],
                "down_sql": ["ALTER TABLE users DROP COLUMN nickname;"], "refusal": None}


class FakeAI(LLMClient):
    answer = ADD_NICKNAME

    def complete(self, system, user, json_schema=None):
        return json.dumps(self.answer)


@pytest.fixture
def client(monkeypatch):
    name = "scratch_" + uuid.uuid4().hex[:8]
    with psycopg.connect(DATABASE_URL, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    url = DATABASE_URL.rsplit("/", 1)[0] + "/" + name
    setup = psycopg.connect(url, autocommit=True, row_factory=dict_row)
    setup.execute("CREATE TABLE users (id bigserial PRIMARY KEY, email text NOT NULL, tier text)")
    setup.execute("INSERT INTO users (email) SELECT 'u' || g FROM generate_series(1, 20) g")
    store.ensure_tables(setup)
    setup.close()

    monkeypatch.setattr(deps, "open_admin", lambda: psycopg.connect(url, autocommit=True, row_factory=dict_row))
    monkeypatch.setattr(deps, "open_readonly", lambda: psycopg.connect(url, row_factory=dict_row))
    monkeypatch.setattr(lifecycle, "dry_run", lambda up, down: SandboxReport(status="ok", message="fine"))
    FakeAI.answer = ADD_NICKNAME
    app.dependency_overrides[deps.llm_client] = lambda: FakeAI()
    yield TestClient(app)
    app.dependency_overrides.clear()
    with psycopg.connect(DATABASE_URL, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


def propose(client):
    r = client.post("/migrations/propose", json={"request": "add tier"}, headers=H)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def apply_all(client):
    mid = propose(client)
    for step in ("sandbox", "approve", "apply"):
        r = client.post(f"/migrations/{mid}/{step}", headers=H)
        assert r.status_code == 200, r.text
    return mid


def tables_columns(client):
    users = next(t for t in client.get("/schema", headers=H).json()["tables"] if t["name"] == "users")
    return [c["name"] for c in users["columns"]]


# ---- security ----
def test_health_needs_no_key(client):
    assert client.get("/health").json() == {"status": "ok"}


@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong"}])
def test_other_endpoints_need_the_right_key(client, headers):
    assert client.get("/schema", headers=headers).status_code == 401
    assert client.get("/migrations", headers=headers).status_code == 401
    assert client.post("/migrations/propose", json={"request": "x y z"}, headers=headers).status_code == 401


def test_browser_on_the_frontend_address_is_allowed(client):
    r = client.options("/migrations", headers={"Origin": "http://localhost:3000", "Access-Control-Request-Method": "GET"})
    assert r.headers["access-control-allow-origin"] == "http://localhost:3000"


# ---- schema and migrations ----
def test_schema_endpoint(client):
    assert tables_columns(client) == ["id", "email", "tier"]


def test_whole_flow_through_the_api(client):
    mid = apply_all(client)
    assert client.get(f"/migrations/{mid}", headers=H).json()["status"] == "APPLIED"
    assert "nickname" in tables_columns(client)
    r = client.post(f"/migrations/{mid}/rollback", headers=H)
    assert r.json()["status"] == "ROLLED_BACK" and "nickname" not in tables_columns(client)


def test_steps_in_the_wrong_order_give_409(client):
    mid = propose(client)
    for step in ("approve", "apply", "rollback"):
        r = client.post(f"/migrations/{mid}/{step}", headers=H)
        assert r.status_code == 409 and "needs" in r.json()["detail"]


def test_unknown_migration_gives_404(client):
    assert client.get("/migrations/99999", headers=H).status_code == 404
    assert client.post("/migrations/99999/approve", headers=H).status_code == 404


def test_refused_request_is_stored_as_rejected(client):
    FakeAI.answer = {"refusal": "Too destructive"}
    r = client.post("/migrations/propose", json={"request": "drop everything"}, headers=H)
    assert r.status_code == 200 and r.json()["status"] == "REJECTED" and r.json()["error"] == "Too destructive"


def test_request_text_is_checked(client):
    assert client.post("/migrations/propose", json={"request": "x"}, headers=H).status_code == 422


def test_list_and_audit(client):
    mid = apply_all(client)
    assert [m["id"] for m in client.get("/migrations", headers=H).json()] == [mid]
    steps = [a["action"] for a in client.get(f"/migrations/{mid}/audit", headers=H).json()]
    assert steps == ["created", "sandbox", "approved", "applied"]


def test_ai_not_reachable_gives_503(client):
    class Down(LLMClient):
        def complete(self, *a, **k):
            raise RuntimeError("Could not reach Ollama. Is it running?")
    app.dependency_overrides[deps.llm_client] = lambda: Down()
    r = client.post("/migrations/propose", json={"request": "add tier"}, headers=H)
    assert r.status_code == 503 and "Ollama" in r.json()["detail"]


# ---- live updates ----
def test_websocket_sends_state_and_closes_when_finished(client):
    mid = apply_all(client)
    with client.websocket_connect(f"/ws/migrations/{mid}?api_key={API_KEY}") as ws:
        state = ws.receive_json()
        assert state["status"] == "APPLIED" and state["steps"][-1] == "applied"


def test_websocket_refuses_a_wrong_key(client):
    with pytest.raises(Exception):
        with client.websocket_connect("/ws/migrations/1?api_key=wrong") as ws:
            ws.receive_json()


def test_websocket_unknown_migration(client):
    with client.websocket_connect(f"/ws/migrations/99999?api_key={API_KEY}") as ws:
        assert "not found" in ws.receive_json()["error"]


# ---- backfills ----
def test_backfill_flow(client):
    body = {"request": "make some users VIP", "table": "users", "set_clause": "tier = 'VIP'",
            "where_clause": "id <= 12", "batch_size": 5}
    created = client.post("/backfills", json=body, headers=H)
    assert created.status_code == 200 and created.json()["rows_to_update"] == 12
    bid = created.json()["id"]
    r = client.post(f"/backfills/{bid}/run", headers=H)
    assert r.status_code == 202
    done = client.get(f"/backfills/{bid}", headers=H).json()   # the background job has finished
    assert done["status"] == "DONE" and done["rows_updated"] == 12
    assert client.post(f"/backfills/{bid}/run", headers=H).status_code == 409   # cannot run twice
    assert [b["id"] for b in client.get("/backfills", headers=H).json()] == [bid]


def test_bad_backfill_gives_400(client):
    body = {"request": "bad one", "table": "users", "set_clause": "ghost = 1", "where_clause": "true"}
    r = client.post("/backfills", json=body, headers=H)
    assert r.status_code == 400 and "ghost" in r.json()["detail"]


def test_the_api_lists_all_the_endpoints(client):
    paths = client.get("/openapi.json").json()["paths"]
    for p in ("/schema", "/migrations/propose", "/migrations/{migration_id}/apply", "/backfills/{backfill_id}/run"):
        assert p in paths


def test_docs_page_offers_an_authorize_button(client):
    schemes = client.get("/openapi.json").json()["components"]["securitySchemes"]
    assert any(v["type"] == "apiKey" and v["name"] == "X-API-Key" for v in schemes.values())
