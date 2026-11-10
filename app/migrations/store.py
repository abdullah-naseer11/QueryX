"""Where migrations are remembered: two tables in a separate schema called 'dbarch'
(so they never mix with your own tables)."""
from datetime import datetime

import psycopg
from pydantic import BaseModel

SCHEMA_SQL = """
CREATE SCHEMA IF NOT EXISTS dbarch;
CREATE TABLE IF NOT EXISTS dbarch.migrations (
    id bigserial PRIMARY KEY,
    request text NOT NULL,
    summary text NOT NULL DEFAULT '',
    up_sql text NOT NULL DEFAULT '',
    down_sql text NOT NULL DEFAULT '',
    status text NOT NULL,
    safety_verdict text,
    sandbox_status text,
    error text,
    created_by text,
    approved_by text,
    created_at timestamptz NOT NULL DEFAULT now(),
    approved_at timestamptz,
    applied_at timestamptz
);
CREATE TABLE IF NOT EXISTS dbarch.audit_log (
    id bigserial PRIMARY KEY,
    migration_id bigint REFERENCES dbarch.migrations(id),
    action text NOT NULL,
    actor text NOT NULL,
    detail text NOT NULL DEFAULT '',
    at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS dbarch.backfills (
    id bigserial PRIMARY KEY,
    request text NOT NULL,
    summary text NOT NULL DEFAULT '',
    table_name text NOT NULL,
    key_column text NOT NULL,
    set_clause text NOT NULL,
    where_clause text NOT NULL,
    batch_size int NOT NULL,
    status text NOT NULL,
    min_key bigint,
    max_key bigint,
    last_key bigint,
    rows_to_update bigint NOT NULL DEFAULT 0,
    rows_updated bigint NOT NULL DEFAULT 0,
    error text,
    created_by text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
"""

EDITABLE = {"error", "sandbox_status", "approved_by"}


class Migration(BaseModel):
    id: int
    request: str
    summary: str
    up_sql: str
    down_sql: str
    status: str
    safety_verdict: str | None = None
    sandbox_status: str | None = None
    error: str | None = None
    created_by: str | None = None
    approved_by: str | None = None
    created_at: datetime | None = None
    approved_at: datetime | None = None
    applied_at: datetime | None = None


def ensure_tables(conn: psycopg.Connection) -> None:
    conn.execute(SCHEMA_SQL)


def insert(conn, request: str, summary: str, up_sql: str, down_sql: str, status: str,
           verdict: str | None, error: str | None, actor: str) -> int:
    row = conn.execute(
        """INSERT INTO dbarch.migrations (request, summary, up_sql, down_sql, status, safety_verdict, error, created_by)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
        (request, summary, up_sql, down_sql, status, verdict, error, actor)).fetchone()
    return row["id"]


def get(conn, migration_id: int) -> Migration:
    row = conn.execute("SELECT * FROM dbarch.migrations WHERE id = %s", (migration_id,)).fetchone()
    if not row:
        raise KeyError(f"No migration with id {migration_id}")
    return Migration(**row)


def list_all(conn) -> list[Migration]:
    return [Migration(**r) for r in conn.execute("SELECT * FROM dbarch.migrations ORDER BY id DESC")]


def move(conn, migration_id: int, allowed: tuple[str, ...], new: str, **fields) -> None:
    """Change the status, but ONLY if it is currently one of `allowed`. This is the rule keeper."""
    sets, values = ["status = %s"], [new]
    for key, value in fields.items():
        assert key in EDITABLE, key
        sets.append(f"{key} = %s")
        values.append(value)
    if new == "APPROVED":
        sets.append("approved_at = now()")
    if new == "APPLIED":
        sets.append("applied_at = now()")
    row = conn.execute(
        f"UPDATE dbarch.migrations SET {', '.join(sets)} WHERE id = %s AND status = ANY(%s) RETURNING id",
        (*values, migration_id, list(allowed))).fetchone()
    if not row:
        current = get(conn, migration_id).status
        raise ValueError(f"Migration {migration_id} is {current}. This step needs: {' or '.join(allowed)}.")


def log(conn, migration_id: int, action: str, actor: str, detail: str = "") -> None:
    conn.execute("INSERT INTO dbarch.audit_log (migration_id, action, actor, detail) VALUES (%s, %s, %s, %s)",
                 (migration_id, action, actor, detail))


def audit_log(conn, migration_id: int) -> list[dict]:
    return list(conn.execute("SELECT action, actor, detail, at FROM dbarch.audit_log WHERE migration_id = %s ORDER BY id",
                             (migration_id,)))
