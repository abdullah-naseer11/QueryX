"""A backfill is described as DATA (table, what to set, which rows), not as free SQL.
We turn it into one UPDATE per batch, and check the result with the SQL parser first."""
from pglast import ast, parse_sql
from pglast.parser import ParseError
from pydantic import BaseModel

from app.db.models import SchemaSnapshot

INTEGER_TYPES = {"smallint", "integer", "bigint"}


class BackfillSpec(BaseModel):
    summary: str = ""
    table: str = ""
    key: str = "id"          # the column used to cut the table into ranges (its primary key)
    set_clause: str = ""     # for example: user_tier = 'VIP'
    where_clause: str = ""   # for example: id IN (SELECT user_id FROM orders GROUP BY user_id HAVING sum(total) > 1000)
    refusal: str | None = None


def render(spec: BackfillSpec, low: int, high: int) -> str:
    """The UPDATE for one batch: rows with key > low and key <= high."""
    t, k = f'"{spec.table}"', f'"{spec.key}"'
    return (f"UPDATE {t} SET {spec.set_clause} "
            f"WHERE {k} > {int(low)} AND {k} <= {int(high)} AND ({spec.where_clause})")


def validate_spec(spec: BackfillSpec, snapshot: SchemaSnapshot) -> str | None:
    """Returns a problem description, or None if the spec is fine."""
    table = snapshot.table(spec.table)
    if table is None:
        return f"Table '{spec.table}' does not exist."
    if table.primary_key != [spec.key]:
        return f"'{spec.key}' must be the single-column primary key of {spec.table} (it is {table.primary_key})."
    key_type = next(c.data_type for c in table.columns if c.name == spec.key)
    if key_type not in INTEGER_TYPES:
        return f"The key column must be a whole number type, not {key_type}."
    if not spec.set_clause.strip() or not spec.where_clause.strip():
        return "Both set_clause and where_clause are required (a backfill must say which rows it changes)."
    try:
        statements = parse_sql(render(spec, 0, 1))
    except ParseError as e:
        return f"The SQL could not be parsed: {e}"
    if len(statements) != 1 or not isinstance(statements[0].stmt, ast.UpdateStmt):
        return "set_clause and where_clause must make exactly ONE UPDATE statement."
    stmt = statements[0].stmt
    if stmt.relation.relname != spec.table or stmt.relation.schemaname:
        return "The UPDATE must change only the named table."
    columns = {c.name for c in table.columns}
    for target in stmt.targetList:
        if target.name not in columns:
            return f"Column '{target.name}' does not exist in {spec.table}."
        if target.name == spec.key:
            return "A backfill must not change the key column."
    return None
