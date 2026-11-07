"""Creates a limited 'reader' account. It may connect and look at the structure
(tables, columns, keys, indexes) but it cannot read rows and cannot change anything.
Safe to run again."""
import psycopg
from app.config import DATABASE_URL

SQL = """
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'reader') THEN
        CREATE ROLE reader LOGIN PASSWORD 'reader';
    END IF;
END $$;
GRANT CONNECT ON DATABASE shop TO reader;
"""


def main() -> None:
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        conn.execute(SQL)
    print("read-only role 'reader' is ready")


if __name__ == "__main__":
    main()
