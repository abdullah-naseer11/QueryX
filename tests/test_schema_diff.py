from app.db.models import Column, ForeignKey, Index, SchemaSnapshot, Table
from app.db.schema_diff import diff_schemas


def snap(*tables: Table) -> SchemaSnapshot:
    return SchemaSnapshot(schema_name="public", server_version="16", tables=list(tables))


def users(*extra_cols: Column, indexes=(), fks=()) -> Table:
    cols = [Column(name="id", data_type="bigint", nullable=False)] + list(extra_cols)
    return Table(name="users", columns=cols, primary_key=["id"], indexes=list(indexes), foreign_keys=list(fks))


def kinds(a, b) -> list[str]:
    return [c.kind for c in diff_schemas(a, b).changes]


def test_same_schema_has_no_changes():
    assert diff_schemas(snap(users()), snap(users())).is_empty


def test_column_added():
    after = snap(users(Column(name="tier", data_type="text", nullable=True)))
    assert kinds(snap(users()), after) == ["column_added"]


def test_column_removed():
    before = snap(users(Column(name="tier", data_type="text", nullable=True)))
    assert kinds(before, snap(users())) == ["column_removed"]


def test_column_type_changed():
    a = snap(users(Column(name="n", data_type="text", nullable=True)))
    b = snap(users(Column(name="n", data_type="integer", nullable=True)))
    d = diff_schemas(a, b)
    assert [c.kind for c in d.changes] == ["column_changed"]
    assert "text -> integer" in d.changes[0].detail


def test_column_became_not_null():
    a = snap(users(Column(name="n", data_type="text", nullable=True)))
    b = snap(users(Column(name="n", data_type="text", nullable=False)))
    assert "nullable" in diff_schemas(a, b).changes[0].detail


def test_index_added_and_removed():
    ix = Index(name="i1", unique=False, primary=False, definition="CREATE INDEX i1 ON users (id)")
    assert kinds(snap(users()), snap(users(indexes=[ix]))) == ["index_added"]
    assert kinds(snap(users(indexes=[ix])), snap(users())) == ["index_removed"]


def test_foreign_key_added():
    fk = ForeignKey(name="fk1", columns=["id"], ref_table="other", ref_columns=["id"])
    assert kinds(snap(users()), snap(users(fks=[fk]))) == ["foreign_key_added"]


def test_table_added_and_removed():
    other = Table(name="other", columns=[Column(name="id", data_type="bigint", nullable=False)])
    assert kinds(snap(users()), snap(users(), other)) == ["table_added"]
    assert kinds(snap(users(), other), snap(users())) == ["table_removed"]


def test_row_estimates_are_ignored():
    a, b = users(), users()
    b.estimated_rows = 999
    assert diff_schemas(snap(a), snap(b)).is_empty


def test_text_output_is_readable():
    after = snap(users(Column(name="tier", data_type="text", nullable=True)))
    assert diff_schemas(snap(users()), after).text() == "+ column users: tier text"
