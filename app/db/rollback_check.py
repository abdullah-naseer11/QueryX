"""Proves a rollback works: run the migration, run the rollback, and the schema must be
exactly as it was before. Everything runs inside a transaction that is always cancelled,
so nothing is kept in the database. (PostgreSQL can undo schema changes too.)"""
from typing import Literal

import psycopg
from pydantic import BaseModel

from app.db.schema_diff import SchemaDiff, diff_schemas
from app.db.schema_reader import read_schema


class RollbackCheck(BaseModel):
    status: Literal["ok", "mismatch", "error", "skipped"]
    message: str = ""
    migration_diff: SchemaDiff = SchemaDiff()   # what the migration changed
    leftover_diff: SchemaDiff = SchemaDiff()    # what the rollback failed to undo


def verify_rollback(conn: psycopg.Connection, up_sql: str, down_sql: str, schema: str = "public") -> RollbackCheck:
    """`conn` needs permission to change the schema (use a sandbox or test database)."""
    both = (up_sql + down_sql).upper()
    if "CONCURRENTLY" in both:
        return RollbackCheck(status="skipped",
                             message="CONCURRENTLY cannot run inside a transaction. It will be tested in the sandbox.")
    try:
        with conn.transaction(force_rollback=True):  # always cancelled at the end
            before = read_schema(conn, schema)
            conn.execute(up_sql)
            after_up = read_schema(conn, schema)
            conn.execute(down_sql)
            after_down = read_schema(conn, schema)
    except psycopg.Error as e:
        return RollbackCheck(status="error", message=f"{type(e).__name__}: {e}".strip())

    migration_diff = diff_schemas(before, after_up)
    leftover = diff_schemas(before, after_down)
    if migration_diff.is_empty:
        return RollbackCheck(status="mismatch", message="The migration changed nothing.", migration_diff=migration_diff)
    if not leftover.is_empty:
        return RollbackCheck(status="mismatch", message="The rollback did not restore the original schema.",
                             migration_diff=migration_diff, leftover_diff=leftover)
    return RollbackCheck(status="ok", message="Rollback restores the original schema.", migration_diff=migration_diff)
