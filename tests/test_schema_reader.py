import psycopg
import pytest

from app.db.connection import connect
from app.db.schema_reader import read_schema, to_prompt_text


@pytest.fixture(scope="module")
def snap():
    with connect(readonly=True) as conn:
        return read_schema(conn)


def test_finds_all_four_tables(snap):
    assert [t.name for t in snap.tables] == ["order_items", "orders", "products", "users"]


def test_columns_and_nullability(snap):
    users = snap.table("users")
    email = next(c for c in users.columns if c.name == "email")
    assert email.data_type == "text"
    assert email.nullable is False


def test_primary_keys(snap):
    assert snap.table("users").primary_key == ["id"]


def test_foreign_keys(snap):
    fk = snap.table("orders").foreign_keys[0]
    assert fk.columns == ["user_id"]
    assert fk.ref_table == "users"
    assert fk.ref_columns == ["id"]


def test_indexes_include_unique_email(snap):
    users = snap.table("users")
    assert any(i.unique and "email" in i.definition for i in users.indexes)


def test_unique_constraint_on_email(snap):
    assert ["email"] in snap.table("users").unique_constraints


def test_prompt_text_is_short_and_readable(snap):
    text = to_prompt_text(snap)
    assert "TABLE users" in text
    assert "FOREIGN KEY (user_id) -> users(id)" in text
    assert len(text) < 4000


def test_reader_cannot_read_rows():
    with connect(readonly=True) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT * FROM users")


def test_reader_cannot_change_anything():
    with connect(readonly=True) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("CREATE TABLE hacked (id int)")
