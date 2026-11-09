import pytest

from app.db.connection import connect
from app.db.rollback_check import verify_rollback
from app.db.schema_reader import read_schema

UP = "ALTER TABLE users ADD COLUMN tier text;"


@pytest.fixture
def conn():
    with connect(readonly=False) as c:
        yield c


def test_good_rollback_is_ok(conn):
    r = verify_rollback(conn, UP, "ALTER TABLE users DROP COLUMN tier;")
    assert r.status == "ok"
    assert [c.kind for c in r.migration_diff.changes] == ["column_added"]


def test_rollback_that_does_nothing_is_caught(conn):
    r = verify_rollback(conn, UP, "SELECT 1;")
    assert r.status == "mismatch"
    assert not r.leftover_diff.is_empty


def test_rollback_that_removes_the_wrong_column_is_caught(conn):
    r = verify_rollback(conn, UP, "ALTER TABLE users DROP COLUMN name;")
    assert r.status == "mismatch"


def test_migration_that_changes_nothing_is_caught(conn):
    assert verify_rollback(conn, "SELECT 1;", "SELECT 1;").status == "mismatch"


def test_sql_error_is_reported_not_raised(conn):
    r = verify_rollback(conn, "ALTER TABLE no_such_table ADD COLUMN x text;", "SELECT 1;")
    assert r.status == "error" and "no_such_table" in r.message


def test_concurrently_is_skipped(conn):
    r = verify_rollback(conn, "CREATE INDEX CONCURRENTLY i1 ON users (email);", "DROP INDEX CONCURRENTLY i1;")
    assert r.status == "skipped"


def test_nothing_is_kept_in_the_database(conn):
    before = [c.name for c in read_schema(conn).table("users").columns]
    verify_rollback(conn, UP, "SELECT 1;")  # a bad rollback: the column would stay if anything were kept
    after = [c.name for c in read_schema(conn).table("users").columns]
    assert before == after and "tier" not in after
