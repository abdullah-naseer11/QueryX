"""Command line. Run:  python -m app --help

Looking:    schema | check "SQL" | verify "UP" "DOWN" | dryrun "UP" "DOWN" | propose "request"
Migrations: new "request" -> sandbox ID -> approve ID -> apply ID   (then rollback ID if needed)
            list | show ID | reject ID"""
import argparse
import getpass
import sys
from contextlib import contextmanager

from app.db.connection import connect
from app.db.rollback_check import verify_rollback
from app.db.schema_reader import read_schema, to_prompt_text
from app.llm.agent import join_sql, propose_migration
from app.llm.client import OllamaClient
from app.migrations import lifecycle, store
from app.safety.checker import check_sql
from app.sandbox.runner import dry_run


@contextmanager
def admin_session():
    """Admin connection (can change things). Used only for migrations."""
    with connect(readonly=False) as conn:
        conn.autocommit = True
        store.ensure_tables(conn)
        yield conn


def show(m: store.Migration) -> None:
    print(f"\n#{m.id}  STATUS: {m.status}\nRequest: {m.request}")
    if m.summary:
        print(f"Summary: {m.summary}")
    if m.up_sql:
        print(f"\nMIGRATION:\n{m.up_sql}\n\nROLLBACK:\n{m.down_sql}")
    print(f"\nSafety: {m.safety_verdict}   Sandbox: {m.sandbox_status}   Approved by: {m.approved_by}")
    if m.error:
        print(f"Problem: {m.error}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="app")
    sub = parser.add_subparsers(dest="command", required=True)

    s = sub.add_parser("schema", help="print the database structure")
    s.add_argument("--json", action="store_true")
    s.add_argument("--schema", default="public")
    c = sub.add_parser("check", help="safety check for SQL")
    c.add_argument("sql", nargs="?")
    c.add_argument("--file")
    for name, text in (("verify", "test a rollback (nothing is kept)"), ("dryrun", "test in a throw-away Docker database")):
        x = sub.add_parser(name, help=text)
        x.add_argument("up")
        x.add_argument("down")
    sub.add_parser("propose", help="AI writes a migration (only shows it)").add_argument("request")

    sub.add_parser("new", help="AI writes a migration and it is stored").add_argument("request")
    sub.add_parser("list", help="all migrations")
    for name, text in (("show", "details of one migration"), ("sandbox", "test it in the Docker sandbox"),
                       ("approve", "approve it (after the sandbox)"), ("reject", "reject it"),
                       ("apply", "run it on the REAL database (needs approval)"), ("rollback", "undo an applied migration")):
        sub.add_parser(name, help=text).add_argument("id", type=int)

    args = parser.parse_args()
    actor = getpass.getuser()

    try:
        if args.command == "schema":
            with connect(readonly=True) as conn:
                snap = read_schema(conn, args.schema)
            print(snap.model_dump_json(indent=2) if args.json else to_prompt_text(snap))

        elif args.command == "check":
            sql = open(args.file, encoding="utf-8").read() if args.file else args.sql
            if not sql:
                sys.exit("Give SQL text or --file")
            report = check_sql(sql)
            print(f"VERDICT: {report.verdict}")
            for r in report.results:
                print(f"\n[{r.severity}] {r.rule}\n  Problem : {r.message}\n  Safer   : {r.suggestion}\n  SQL     : {r.statement}")

        elif args.command == "verify":
            with connect(readonly=False) as conn:
                r = verify_rollback(conn, args.up, args.down)
            print(f"ROLLBACK CHECK: {r.status}\n{r.message}")
            if not r.migration_diff.is_empty:
                print("\nWhat the migration changed:\n" + r.migration_diff.text())
            if not r.leftover_diff.is_empty:
                print("\nWhat the rollback failed to undo:\n" + r.leftover_diff.text())

        elif args.command == "dryrun":
            print("Starting the sandbox (first time can take a minute)...")
            r = dry_run(args.up, args.down)
            print(f"\nSANDBOX: {r.status}\n{r.message}")
            if r.error_at:
                print(f"(failed at: {r.error_at})")
            print(f"Migration took {r.up_seconds}s, rollback took {r.down_seconds}s")
            if not r.migration_diff.is_empty:
                print("\nWhat the migration changed:\n" + r.migration_diff.text())
            if not r.leftover_diff.is_empty:
                print("\nWhat the rollback failed to undo:\n" + r.leftover_diff.text())
            print("\nNote: " + r.note)

        elif args.command == "propose":
            with connect(readonly=True) as conn:
                schema_text = to_prompt_text(read_schema(conn))
            r = propose_migration(args.request, schema_text, OllamaClient())
            print(f"STATUS: {r.status}  (tries: {r.attempts})")
            if r.message:
                print(r.message)
            if r.proposal and r.proposal.up_sql:
                print(f"\nSUMMARY: {r.proposal.summary}\n\nMIGRATION:\n{join_sql(r.proposal.up_sql)}\n\nROLLBACK:\n{join_sql(r.proposal.down_sql)}")
            if r.report:
                print(f"\nSAFETY: {r.report.verdict}")
                for x in r.report.results:
                    print(f"  [{x.severity}] {x.message}")

        else:  # migration commands
            with admin_session() as conn:
                if args.command == "new":
                    with connect(readonly=True) as ro:
                        schema_text = to_prompt_text(read_schema(ro))
                    m = lifecycle.create(conn, args.request, schema_text, OllamaClient(), actor)
                    show(m)
                    if m.status == "CHECKED":
                        print(f"\nNext: python -m app sandbox {m.id}")
                elif args.command == "list":
                    for m in store.list_all(conn):
                        print(f"#{m.id:<4} {m.status:<14} {m.request}")
                elif args.command == "show":
                    show(store.get(conn, args.id))
                else:
                    if args.command == "sandbox":
                        print("Starting the sandbox (first time can take a minute)...")
                    m = getattr(lifecycle, args.command)(conn, args.id, actor)
                    show(m)
    except (ValueError, KeyError, RuntimeError) as e:
        sys.exit(f"\nSTOPPED: {e}")


if __name__ == "__main__":
    main()
