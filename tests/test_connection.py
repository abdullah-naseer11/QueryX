import psycopg
from app.config import DATABASE_URL


def test_can_connect():
    with psycopg.connect(DATABASE_URL) as conn:
        assert conn.execute("SELECT 1").fetchone()[0] == 1


def test_sample_data_exists():
    with psycopg.connect(DATABASE_URL) as conn:
        count = conn.execute("SELECT count(*) FROM users").fetchone()[0]
    assert count > 1000
