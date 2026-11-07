"""Command line:  python -m app schema        (readable text)
                 python -m app schema --json (full details)"""
import argparse

from app.db.connection import connect
from app.db.schema_reader import read_schema, to_prompt_text


def main() -> None:
    parser = argparse.ArgumentParser(prog="app")
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("schema", help="print the database structure")
    s.add_argument("--json", action="store_true", help="print full JSON instead of short text")
    s.add_argument("--schema", default="public", help="schema name (default: public)")
    args = parser.parse_args()

    if args.command == "schema":
        with connect(readonly=True) as conn:
            snap = read_schema(conn, args.schema)
        print(snap.model_dump_json(indent=2) if args.json else to_prompt_text(snap))


if __name__ == "__main__":
    main()
