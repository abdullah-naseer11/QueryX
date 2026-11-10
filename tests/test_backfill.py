import json
import uuid

import psycopg
import pytest
from psycopg.rows import dict_row

from app.backfill import engine
from app.backfill.propose import propose_backfill
from app.backfill.spec import BackfillSpec, validate_spec
from app.config import DATABASE_URL
from app.db.schema_reader import read_schema
from app.llm.client import LLMClient
from app.migrations import store


@pytest.fixture
def db():
    name = "scratch_" + uuid.uuid4().hex[:8]
    with psycopg.connect(DATABASE_URL, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    url = DATABASE_URL.rsplit("/", 1)[0] + "/" + name
    conn = psycopg.connect(url, autocommit=True, row_factory=dict_row)
    conn.execute("CREATE TABLE users (id bigserial PRIMARY KEY, email text NOT NULL, tier text, note text)")
    conn.execute("INSERT INTO users (email) SELECT 'u' || g || '@x.com' FROM generate_series(1, 25) g")
    conn.execute("CREATE TABLE tags (name text PRIMARY KEY)")
    store.ensure_tables(conn)
    conn.url = url
    yield conn
    conn.close()
    with psycopg.connect(DATABASE_URL, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


def spec(set_clause="tier = 'VIP'", where="id % 2 = 0", **kw):
    return BackfillSpec(table="users", key="id", set_clause=set_clause, where_clause=where, **kw)


def make(db, s=None, batch=10):
    return engine.create(db, "req", s or spec(), read_schema(db), "tester", batch_size=batch)


def count(db, where="tier = 'VIP'"):
    return db.execute(f"SELECT count(*) AS n FROM users WHERE {where}").fetchone()["n"]


# ---------- the spec check ----------
def test_valid_spec_passes(db):
    assert validate_spec(spec(), read_schema(db)) is None


@pytest.mark.parametrize("bad,part", [
    (BackfillSpec(table="nope", set_clause="a = 1", where_clause="true"), "does not exist"),
    (spec(set_clause="ghost = 1"), "ghost"),
    (spec(set_clause="id = 5"), "key column"),
    (spec(where=""), "required"),
    (spec(set_clause="tier = 'x'; DROP TABLE users"), ""),  # a second statement is never accepted
    (spec(where="true); DROP TABLE users; --"), ""),
    (BackfillSpec(table="tags", key="name", set_clause="name = 'a'", where_clause="true"), "whole number"),
])
def test_bad_specs_are_refused(db, bad, part):
    problem = validate_spec(bad, read_schema(db))
    assert problem and part in problem


def test_percent_signs_in_conditions_are_fine(db):
    s = spec(where="email LIKE '%1@x.com'")
    assert validate_spec(s, read_schema(db)) is None
    b = make(db, s)
    assert engine.run(db, b.id, sleep=0).status == "DONE"
    assert count(db) == 3  # u1, u11, u21


# ---------- running ----------
def test_create_counts_rows_but_changes_nothing(db):
    b = make(db)
    assert (b.status, b.rows_to_update, b.min_key, b.max_key) == ("PENDING", 12, 1, 25)
    assert count(db) == 0


def test_run_updates_only_matching_rows_in_batches(db):
    b = make(db, batch=10)
    seen = []
    done = engine.run(db, b.id, sleep=0, progress=lambda x: seen.append(x.rows_updated))
    assert done.status == "DONE" and done.rows_updated == 12
    assert count(db) == 12 and count(db, "tier IS NULL") == 13
    assert len(seen) == 3  # ids 1-10, 11-20, 21-30


def test_stop_and_continue_without_doing_work_twice(db):
    b = make(db, batch=10)
    paused = engine.run(db, b.id, sleep=0, max_batches=1)
    assert paused.status == "PAUSED" and paused.last_key == 10 and paused.rows_updated == 5
    done = engine.run(db, b.id, sleep=0)
    assert done.status == "DONE" and done.rows_updated == 12 and count(db) == 12


def test_ctrl_c_pauses_and_can_continue(db):
    b = make(db, batch=10)

    def interrupt(_):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        engine.run(db, b.id, sleep=0, progress=interrupt)
    assert engine.get(db, b.id).status == "PAUSED"
    assert engine.run(db, b.id, sleep=0).status == "DONE"
    assert count(db) == 12


def test_runtime_error_fails_cleanly_and_keeps_progress_safe(db):
    b = make(db, spec(set_clause="tier = (1/0)::text"))
    r = engine.run(db, b.id, sleep=0)
    assert r.status == "FAILED" and "division" in r.error and r.rows_updated == 0
    assert count(db, "tier IS NOT NULL") == 0


def test_failed_backfill_can_be_fixed_and_continued(db):
    b = make(db, batch=10)
    db.execute("ALTER TABLE users RENAME COLUMN tier TO tier_old")  # break it
    assert engine.run(db, b.id, sleep=0).status == "FAILED"
    db.execute("ALTER TABLE users RENAME COLUMN tier_old TO tier")  # fix it
    assert engine.run(db, b.id, sleep=0).status == "DONE" and count(db) == 12


def test_finished_backfill_cannot_run_again(db):
    b = make(db)
    engine.run(db, b.id, sleep=0)
    with pytest.raises(ValueError, match="needs"):
        engine.run(db, b.id, sleep=0)


def test_empty_table_finishes_at_once(db):
    db.execute("DELETE FROM users")
    b = make(db)
    assert engine.run(db, b.id, sleep=0).status == "DONE"


def test_lock_on_the_table_fails_cleanly(db):
    b = make(db)  # create it first: counting rows would also wait for the lock
    blocker = psycopg.connect(db.url)
    blocker.execute("LOCK TABLE users IN ACCESS EXCLUSIVE MODE")
    try:
        r = engine.run(db, b.id, sleep=0, lock_timeout="200ms", retries=2, retry_wait=0)
    finally:
        blocker.close()
    assert r.status == "FAILED" and "LockNotAvailable" in r.error and count(db) == 0


def test_same_backfill_cannot_run_twice_at_once(db):
    b = make(db)
    other = psycopg.connect(db.url, autocommit=True, row_factory=dict_row)
    other.execute("SELECT pg_advisory_lock(%s)", (engine.ADVISORY_BASE + b.id,))
    try:
        with pytest.raises(RuntimeError, match="already running"):
            engine.run(db, b.id, sleep=0)
    finally:
        other.close()


def test_a_bigger_table_in_real_batches(db):
    db.execute("INSERT INTO users (email) SELECT 'big' || g FROM generate_series(1, 20000) g")
    b = make(db, spec(where="id > 100"), batch=5000)
    done = engine.run(db, b.id, sleep=0)
    assert done.status == "DONE" and count(db) == 20025 - 100


# ---------- plain English -> spec ----------
class FakeAI(LLMClient):
    def __init__(self, *answers):
        self.answers, self.asked = list(answers), []

    def complete(self, system, user, json_schema=None):
        self.asked.append(user)
        return self.answers.pop(0)


def ans(**kw):
    base = {"summary": "s", "table": "users", "key": "id", "set_clause": "tier = 'VIP'", "where_clause": "id < 5", "refusal": None}
    return json.dumps({**base, **kw})


def test_ai_good_answer(db):
    s, msg = propose_backfill("vip", "schema", read_schema(db), FakeAI(ans()))
    assert s and s.table == "users" and msg == ""


def test_ai_bad_answer_is_sent_back_and_fixed(db):
    ai = FakeAI(ans(set_clause="ghost = 1"), ans())
    s, _ = propose_backfill("vip", "schema", read_schema(db), ai)
    assert s is not None and "ghost" in ai.asked[1]


def test_ai_never_valid_gives_up(db):
    s, msg = propose_backfill("vip", "schema", read_schema(db), FakeAI("no", "no", "no"))
    assert s is None and "No valid backfill" in msg


def test_ai_refusal(db):
    s, msg = propose_backfill("wipe", "schema", read_schema(db), FakeAI(ans(refusal="Not a data update")))
    assert s is None and msg == "Not a data update"
