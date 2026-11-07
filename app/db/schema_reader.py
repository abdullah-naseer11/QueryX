"""Reads the structure of a PostgreSQL schema using the system catalogs (pg_catalog).
The catalogs are used (not information_schema) because they show structure even to
an account that has no permission to read the table rows."""
import psycopg

from app.db.models import Column, ForeignKey, Index, SchemaSnapshot, Table

COLUMNS_SQL = """
SELECT c.relname AS table_name, a.attname AS name,
       format_type(a.atttypid, a.atttypmod) AS data_type,
       NOT a.attnotnull AS nullable,
       pg_get_expr(d.adbin, d.adrelid) AS "default"
FROM pg_attribute a
JOIN pg_class c ON c.oid = a.attrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
WHERE n.nspname = %s AND c.relkind = 'r' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY c.relname, a.attnum
"""

CONSTRAINTS_SQL = """
SELECT con.conname AS name, con.contype AS kind, rel.relname AS table_name,
  (SELECT array_agg(a.attname ORDER BY k.ord)
     FROM unnest(con.conkey) WITH ORDINALITY k(attnum, ord)
     JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.attnum) AS cols,
  frel.relname AS ref_table,
  (SELECT array_agg(a.attname ORDER BY k.ord)
     FROM unnest(con.confkey) WITH ORDINALITY k(attnum, ord)
     JOIN pg_attribute a ON a.attrelid = con.confrelid AND a.attnum = k.attnum) AS ref_cols
FROM pg_constraint con
JOIN pg_class rel ON rel.oid = con.conrelid
JOIN pg_namespace n ON n.oid = rel.relnamespace
LEFT JOIN pg_class frel ON frel.oid = con.confrelid
WHERE n.nspname = %s AND con.contype IN ('p', 'u', 'f')
ORDER BY rel.relname, con.conname
"""

INDEXES_SQL = """
SELECT t.relname AS table_name, i.relname AS name, ix.indisunique AS "unique",
       ix.indisprimary AS "primary", pg_get_indexdef(ix.indexrelid) AS definition
FROM pg_index ix
JOIN pg_class i ON i.oid = ix.indexrelid
JOIN pg_class t ON t.oid = ix.indrelid
JOIN pg_namespace n ON n.oid = t.relnamespace
WHERE n.nspname = %s AND t.relkind = 'r'
ORDER BY t.relname, i.relname
"""

ROWS_SQL = """
SELECT c.relname AS table_name, c.reltuples::bigint AS rows
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = %s AND c.relkind = 'r'
"""


def read_schema(conn: psycopg.Connection, schema: str = "public") -> SchemaSnapshot:
    """Return the full structure of one schema. `conn` must use dict rows (see connect())."""
    tables: dict[str, Table] = {}
    for r in conn.execute(COLUMNS_SQL, (schema,)):
        t = tables.setdefault(r["table_name"], Table(name=r["table_name"], columns=[]))
        t.columns.append(Column(name=r["name"], data_type=r["data_type"],
                                nullable=r["nullable"], default=r["default"]))

    for r in conn.execute(CONSTRAINTS_SQL, (schema,)):
        t = tables[r["table_name"]]
        if r["kind"] == "p":
            t.primary_key = r["cols"]
        elif r["kind"] == "u":
            t.unique_constraints.append(r["cols"])
        else:
            t.foreign_keys.append(ForeignKey(name=r["name"], columns=r["cols"],
                                             ref_table=r["ref_table"], ref_columns=r["ref_cols"]))

    for r in conn.execute(INDEXES_SQL, (schema,)):
        tables[r["table_name"]].indexes.append(
            Index(name=r["name"], unique=r["unique"], primary=r["primary"], definition=r["definition"]))

    for r in conn.execute(ROWS_SQL, (schema,)):
        if r["table_name"] in tables and r["rows"] >= 0:  # -1 means "never counted"
            tables[r["table_name"]].estimated_rows = r["rows"]

    version = conn.execute("SHOW server_version").fetchone()["server_version"]
    return SchemaSnapshot(schema_name=schema, server_version=version,
                          tables=sorted(tables.values(), key=lambda t: t.name))


def to_prompt_text(snapshot: SchemaSnapshot) -> str:
    """Short, readable text version of the schema. This is what the AI will receive in Step 3."""
    lines = [f"PostgreSQL {snapshot.server_version}, schema '{snapshot.schema_name}'"]
    for t in snapshot.tables:
        rows = f" (~{t.estimated_rows} rows)" if t.estimated_rows is not None else ""
        lines.append(f"\nTABLE {t.name}{rows}")
        for c in t.columns:
            extra = "" if c.nullable else " NOT NULL"
            if c.default:
                extra += f" DEFAULT {c.default}"
            lines.append(f"  {c.name} {c.data_type}{extra}")
        if t.primary_key:
            lines.append(f"  PRIMARY KEY ({', '.join(t.primary_key)})")
        for u in t.unique_constraints:
            lines.append(f"  UNIQUE ({', '.join(u)})")
        for fk in t.foreign_keys:
            lines.append(f"  FOREIGN KEY ({', '.join(fk.columns)}) -> {fk.ref_table}({', '.join(fk.ref_columns)})")
        for ix in t.indexes:
            if not ix.primary:
                lines.append(f"  INDEX {ix.definition}")
    return "\n".join(lines)
