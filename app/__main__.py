"""Command line:
  python -m app schema                 (database structure as text; add --json for full details)
  python -m app check "SQL here"       (safety check for SQL)
  python -m app check --file x.sql     (safety check for a file)
  python -m app propose "request"      (AI writes a checked migration)
  python -m app verify "UP SQL" "DOWN SQL"   (test a rollback; nothing is kept)"""
import argparse
import sys

from app.db.connection import connect
from app.db.rollback_check import verify_rollback
from app.db.schema_reader import read_schema, to_prompt_text
from app.llm.agent import join_sql, propose_migration
from app.llm.client import OllamaClient
from app.safety.checker import check_sql


def main() -> None:
    parser = argparse.ArgumentParser(prog="app")
    sub = parser.add_subparsers(dest="command", required=True)

    s = sub.add_parser("schema", help="print the database structure")
    s.add_argument("--json", action="store_true", help="print full JSON instead of short text")
    s.add_argument("--schema", default="public", help="schema name (default: public)")

    c = sub.add_parser("check", help="safety check for SQL")
    c.add_argument("sql", nargs="?", help="the SQL text")
    c.add_argument("--file", help="read the SQL from a file instead")

    p = sub.add_parser("propose", help="AI writes a migration from plain English")
    p.add_argument("request", help="what you want, in plain English")

    v = sub.add_parser("verify", help="test that a rollback undoes a migration")
    v.add_argument("up", help="migration SQL")
    v.add_argument("down", help="rollback SQL")

    args = parser.parse_args()

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

    elif args.command == "propose":
        with connect(readonly=True) as conn:
            schema_text = to_prompt_text(read_schema(conn))
        result = propose_migration(args.request, schema_text, OllamaClient())
        print(f"STATUS: {result.status}  (tries: {result.attempts})")
        if result.message:
            print(result.message)
        if result.proposal and result.proposal.up_sql:
            print(f"\nSUMMARY: {result.proposal.summary}")
            print("\nMIGRATION:\n" + join_sql(result.proposal.up_sql))
            print("\nROLLBACK:\n" + join_sql(result.proposal.down_sql))
        if result.report:
            print(f"\nSAFETY: {result.report.verdict}")
            for r in result.report.results:
                print(f"  [{r.severity}] {r.message}")

    elif args.command == "verify":
        with connect(readonly=False) as conn:  # needs permission to change; everything is cancelled at the end
            result = verify_rollback(conn, args.up, args.down)
        print(f"ROLLBACK CHECK: {result.status}\n{result.message}")
        if not result.migration_diff.is_empty:
            print("\nWhat the migration changed:\n" + result.migration_diff.text())
        if not result.leftover_diff.is_empty:
            print("\nWhat the rollback failed to undo:\n" + result.leftover_diff.text())


if __name__ == "__main__":
    main()
