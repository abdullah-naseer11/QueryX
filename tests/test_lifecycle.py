"""Tests the whole life of a migration on a scratch database. Docker is not needed:
the sandbox step is replaced by a fake that answers instantly."""
import json
import uuid

import psycopg
import pytest
from psycopg.rows import dict_row

from app.config import DATABASE_URL
from app.db.schema_reader import read_schema
from app.llm.client import LLMClient
from app.migrations import lifecycle, store
from app.sandbox.runner import SandboxReport

SCHEMA_TEXT = "TABLE users\n  id bigint\n  email text"


class FakeAI(LLMClient):
    def __init__(self, answer: dict):
        self.text = json.dumps(answer)

    def complete(self, system, user, json_schema=None):
        return self.text


ADD_TIER = {"summary": "Add tier", "up_sql": ["SET lock_timeout = '3s';", "ALTER TABLE users ADD COLUMN tier text;"],
            "down_sql": ["ALTER TABLE users DROP COLUMN tier;"], "refusal": None}


@pytest.fixture
def db(monkeypatch):
    name = "scratch_" + uuid.uuid4().hex[:8]
    with psycopg.connect(DATABASE_URL, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    url = DATABASE_URL.rsplit("/", 1)[0] + "/" + name
    conn = psycopg.connect(url, autocommit=True, row_factory=dict_row)
    conn.execute("CREATE TABLE users (id bigserial PRIMARY KEY, email text NOT NULL)")
    store.ensure_tables(conn)
    conn.url = url
    monkeypatch.setattr(lifecycle, "dry_run", lambda up, down: SandboxReport(status="ok", message="fine"))
    yield conn
    conn.close()
    with psycopg.connect(DATABASE_URL, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


def columns(conn):
    return [c.name for c in read_schema(conn).table("users").columns]


def approved(db, answer=ADD_TIER):
    m = lifecycle.create(db, "add tier", SCHEMA_TEXT, FakeAI(answer), "tester")
    lifecycle.sandbox(db, m.id, "tester")
    return lifecycle.approve(db, m.id, "tester")


def test_create_stores_a_checked_migration(db):
    m = lifecycle.create(db, "add tier", SCHEMA_TEXT, FakeAI(ADD_TIER), "tester")
    assert (m.status, m.safety_verdict) == ("CHECKED", "OK")
    assert "ADD COLUMN tier" in m.up_sql


def test_refused_request_is_rejected(db):
    m = lifecycle.create(db, "drop all", SCHEMA_TEXT, FakeAI({"refusal": "Too destructive"}), "tester")
    assert m.status == "REJECTED" and m.error == "Too destructive" and m.up_sql == ""


def test_blocked_sql_is_rejected(db):
    bad = {"summary": "x", "up_sql": ["DROP TABLE users;"], "down_sql": [], "refusal": None}
    m = lifecycle.create(db, "drop users", SCHEMA_TEXT, FakeAI(bad), "tester")
    assert m.status == "REJECTED" and m.safety_verdict == "BLOCK"


def test_failed_sandbox_stops_the_migration(db, monkeypatch):
    monkeypatch.setattr(lifecycle, "dry_run", lambda u, d: SandboxReport(status="mismatch", message="bad rollback"))
    m = lifecycle.create(db, "add tier", SCHEMA_TEXT, FakeAI(ADD_TIER), "tester")
    m = lifecycle.sandbox(db, m.id, "tester")
    assert m.status == "FAILED" and "bad rollback" in m.error


def test_cannot_approve_before_sandbox(db):
    m = lifecycle.create(db, "add tier", SCHEMA_TEXT, FakeAI(ADD_TIER), "tester")
    with pytest.raises(ValueError, match="needs: SANDBOXED"):
        lifecycle.approve(db, m.id, "tester")


def test_cannot_apply_before_approval(db):
    m = lifecycle.create(db, "add tier", SCHEMA_TEXT, FakeAI(ADD_TIER), "tester")
    lifecycle.sandbox(db, m.id, "tester")
    with pytest.raises(ValueError, match="needs: APPROVED"):
        lifecycle.apply(db, m.id, "tester")
    assert "tier" not in columns(db)


def test_full_flow_changes_the_database(db):
    m = approved(db)
    assert m.approved_by == "tester"
    m = lifecycle.apply(db, m.id, "tester")
    assert m.status == "APPLIED" and m.applied_at is not None
    assert "tier" in columns(db)


def test_rollback_undoes_it(db):
    m = lifecycle.apply(db, approved(db).id, "tester")
    m = lifecycle.rollback(db, m.id, "tester")
    assert m.status == "ROLLED_BACK" and "tier" not in columns(db)


def test_cannot_apply_twice(db):
    m = approved(db)
    lifecycle.apply(db, m.id, "tester")
    with pytest.raises(ValueError, match="needs: APPROVED"):
        lifecycle.apply(db, m.id, "tester")


def test_cannot_roll_back_something_never_applied(db):
    with pytest.raises(ValueError, match="needs: APPLIED"):
        lifecycle.rollback(db, approved(db).id, "tester")


def test_reject_blocks_apply(db):
    m = approved(db)
    lifecycle.reject(db, m.id, "tester")
    with pytest.raises(ValueError):
        lifecycle.apply(db, m.id, "tester")


def test_half_done_migration_leaves_nothing_behind(db):
    two = {"summary": "x", "up_sql": ["ALTER TABLE users ADD COLUMN a text;", "ALTER TABLE users ADD COLUMN b badtype;"],
           "down_sql": ["SELECT 1;"], "refusal": None}
    m = lifecycle.apply(db, approved(db, two).id, "tester")
    assert m.status == "FAILED" and "badtype" in m.error
    assert "a" not in columns(db)  # the first statement was undone too


def test_lock_timeout_fails_cleanly(db):
    blocker = psycopg.connect(db.url)
    blocker.execute("LOCK TABLE users IN ACCESS EXCLUSIVE MODE")  # someone else is using the table
    try:
        m = lifecycle.apply(db, approved(db).id, "tester", lock_timeout="200ms", retries=2, retry_wait=0)
    finally:
        blocker.close()
    assert m.status == "FAILED" and "LockNotAvailable" in m.error
    assert "tier" not in columns(db)


def test_only_one_migration_at_a_time(db):
    other = psycopg.connect(db.url, autocommit=True, row_factory=dict_row)
    other.execute("SELECT pg_advisory_lock(%s)", (lifecycle.ADVISORY_KEY,))
    try:
        with pytest.raises(RuntimeError, match="Another migration"):
            lifecycle.apply(db, approved(db).id, "tester")
    finally:
        other.close()


def test_audit_log_records_every_step_in_order(db):
    m = lifecycle.apply(db, approved(db).id, "tester")
    assert [a["action"] for a in store.audit_log(db, m.id)] == ["created", "sandbox", "approved", "applied"]
    assert all(a["actor"] == "tester" for a in store.audit_log(db, m.id))


def test_concurrent_index_can_be_applied(db):
    idx = {"summary": "x", "up_sql": ["CREATE INDEX CONCURRENTLY ix ON users (email);"],
           "down_sql": ["DROP INDEX CONCURRENTLY ix;"], "refusal": None}
    m = lifecycle.apply(db, approved(db, idx).id, "tester")
    assert m.status == "APPLIED"
    assert any(i.name == "ix" for i in read_schema(db).table("users").indexes)
