"""Command line:
  python -m app schema                 (database structure as text; add --json for full details)
  python -m app check "SQL here"       (safety check for SQL)
  python -m app check --file x.sql     (safety check for a file)"""
import argparse
import sys

from app.db.connection import connect
from app.db.schema_reader import read_schema, to_prompt_text
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


if __name__ == "__main__":
    main()
