"""Tests the dry-run logic on a scratch database (no Docker needed)."""
import uuid

import psycopg
import pytest
from psycopg.rows import dict_row

from app.config import DATABASE_URL
from app.sandbox.runner import execute_dry_run, split_statements


@pytest.fixture
def scratch():
    name = "scratch_" + uuid.uuid4().hex[:8]
    with psycopg.connect(DATABASE_URL, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    url = DATABASE_URL.rsplit("/", 1)[0] + "/" + name
    with psycopg.connect(url, autocommit=True, row_factory=dict_row) as conn:
        conn.execute("CREATE TABLE users (id bigserial PRIMARY KEY, email text NOT NULL, name text)")
        yield conn
    with psycopg.connect(DATABASE_URL, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


def test_good_migration_and_rollback(scratch):
    r = execute_dry_run(scratch, "ALTER TABLE users ADD COLUMN tier text;", "ALTER TABLE users DROP COLUMN tier;")
    assert r.status == "ok"
    assert [c.kind for c in r.migration_diff.changes] == ["column_added"]
    assert r.up_seconds >= 0 and r.down_seconds >= 0


def test_rollback_that_does_nothing_is_a_mismatch(scratch):
    r = execute_dry_run(scratch, "ALTER TABLE users ADD COLUMN tier text;", "SELECT 1;")
    assert r.status == "mismatch" and not r.leftover_diff.is_empty


def test_migration_that_changes_nothing_is_a_mismatch(scratch):
    assert execute_dry_run(scratch, "SELECT 1;", "SELECT 1;").status == "mismatch"


def test_error_in_migration(scratch):
    r = execute_dry_run(scratch, "ALTER TABLE nope ADD COLUMN x text;", "SELECT 1;")
    assert (r.status, r.error_at) == ("error", "migration") and "nope" in r.message


def test_error_in_rollback(scratch):
    r = execute_dry_run(scratch, "ALTER TABLE users ADD COLUMN tier text;", "ALTER TABLE users DROP COLUMN missing;")
    assert (r.status, r.error_at) == ("error", "rollback")


def test_concurrent_index_works_in_the_sandbox(scratch):
    r = execute_dry_run(scratch, "SET lock_timeout = '3s'; CREATE INDEX CONCURRENTLY ix ON users (email);",
                        "DROP INDEX CONCURRENTLY ix;")
    assert r.status == "ok"
    assert [c.kind for c in r.migration_diff.changes] == ["index_added"]


def test_statements_are_split_one_by_one():
    assert split_statements("SELECT 1; SELECT 2;") == ["SELECT 1", "SELECT 2"]
