"""The life of a migration:

  CHECKED -> SANDBOXED -> APPROVED -> APPLYING -> APPLIED -> ROLLING_BACK -> ROLLED_BACK
  (REJECTED or FAILED can end it earlier.)

Each step is only allowed from the right status. So nothing can reach the real database
without being checked, tested in the sandbox, and approved by a person first.
`conn` must be an ADMIN connection with autocommit on (see admin_session in __main__)."""
import re
import time

import psycopg

from app.llm.agent import join_sql, propose_migration
from app.llm.client import LLMClient
from app.migrations import store
from app.migrations.store import Migration
from app.sandbox.runner import dry_run, split_statements

ADVISORY_KEY = 8675309  # only one migration may run at a time (this number is just a lock name)


def create(conn, request: str, schema_text: str, client: LLMClient, actor: str) -> Migration:
    """Ask the AI, safety-check the answer, and store it."""
    result = propose_migration(request, schema_text, client)
    p = result.proposal
    if result.status == "ok":
        mig_id = store.insert(conn, request, p.summary, join_sql(p.up_sql), join_sql(p.down_sql),
                              "CHECKED", result.report.verdict, None, actor)
    else:
        sql = (join_sql(p.up_sql), join_sql(p.down_sql)) if p and p.up_sql else ("", "")
        mig_id = store.insert(conn, request, p.summary if p else "", *sql, "REJECTED",
                              result.report.verdict if result.report else None, result.message, actor)
    store.log(conn, mig_id, "created", actor, result.status)
    return store.get(conn, mig_id)


def sandbox(conn, migration_id: int, actor: str) -> Migration:
    m = store.get(conn, migration_id)
    if m.status != "CHECKED":
        raise ValueError(f"Migration {migration_id} is {m.status}. This step needs: CHECKED.")
    report = dry_run(m.up_sql, m.down_sql)
    if report.status == "ok":
        store.move(conn, migration_id, ("CHECKED",), "SANDBOXED", sandbox_status="ok")
    else:
        store.move(conn, migration_id, ("CHECKED",), "FAILED", sandbox_status=report.status, error=report.message)
    store.log(conn, migration_id, "sandbox", actor, report.status)
    return store.get(conn, migration_id)


def approve(conn, migration_id: int, actor: str) -> Migration:
    store.move(conn, migration_id, ("SANDBOXED",), "APPROVED", approved_by=actor)
    store.log(conn, migration_id, "approved", actor)
    return store.get(conn, migration_id)


def reject(conn, migration_id: int, actor: str) -> Migration:
    store.move(conn, migration_id, ("CHECKED", "SANDBOXED", "APPROVED"), "REJECTED", error="Rejected by " + actor)
    store.log(conn, migration_id, "rejected", actor)
    return store.get(conn, migration_id)


def _execute(conn, sql: str, lock_timeout: str, retries: int, retry_wait: float) -> None:
    """Run SQL on the real database, carefully: short lock wait, retries, and all-or-nothing when possible."""
    if not re.fullmatch(r"\d+(ms|s|min)", lock_timeout):
        raise ValueError("lock_timeout must look like 3s or 500ms")
    statements = split_statements(sql)
    concurrent = any("CONCURRENTLY" in s.upper() for s in statements)  # cannot run inside a transaction
    conn.execute(f"SET lock_timeout = '{lock_timeout}'")
    conn.execute("SET statement_timeout = '5min'")
    try:
        for attempt in range(1, retries + 1):
            try:
                if concurrent:
                    for s in statements:
                        conn.execute(s)
                else:
                    with conn.transaction():  # if any statement fails, none of them stay
                        for s in statements:
                            conn.execute(s)
                return
            except psycopg.errors.LockNotAvailable:
                if attempt == retries:
                    raise
                time.sleep(retry_wait)
    finally:
        conn.execute("RESET lock_timeout")
        conn.execute("RESET statement_timeout")


def _run_guarded(conn, migration_id, sql, actor, busy, done, failed, action,
                 lock_timeout, retries, retry_wait) -> Migration:
    if not conn.execute("SELECT pg_try_advisory_lock(%s) AS got", (ADVISORY_KEY,)).fetchone()["got"]:
        raise RuntimeError("Another migration is running right now. Try again in a moment.")
    try:
        try:
            _execute(conn, sql, lock_timeout, retries, retry_wait)
        except psycopg.Error as e:
            store.move(conn, migration_id, (busy,), failed, error=f"{type(e).__name__}: {e}".strip())
            store.log(conn, migration_id, action + "_failed", actor, str(e).strip()[:300])
        else:
            store.move(conn, migration_id, (busy,), done)
            store.log(conn, migration_id, action, actor)
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_KEY,))
    return store.get(conn, migration_id)


def apply(conn, migration_id: int, actor: str, lock_timeout: str = "3s", retries: int = 3, retry_wait: float = 2.0) -> Migration:
    """Run the migration on the real database. Only an APPROVED migration is allowed."""
    store.move(conn, migration_id, ("APPROVED",), "APPLYING")  # also stops a second apply of the same one
    m = store.get(conn, migration_id)
    return _run_guarded(conn, migration_id, m.up_sql, actor, "APPLYING", "APPLIED", "FAILED", "applied",
                        lock_timeout, retries, retry_wait)


def rollback(conn, migration_id: int, actor: str, lock_timeout: str = "3s", retries: int = 3, retry_wait: float = 2.0) -> Migration:
    store.move(conn, migration_id, ("APPLIED",), "ROLLING_BACK")
    m = store.get(conn, migration_id)
    return _run_guarded(conn, migration_id, m.down_sql, actor, "ROLLING_BACK", "ROLLED_BACK", "ROLLBACK_FAILED",
                        "rolled_back", lock_timeout, retries, retry_wait)
