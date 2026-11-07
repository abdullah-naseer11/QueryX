"""Database connections. Two accounts: a read-only one (default) and an admin one."""
from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.rows import dict_row

from app.config import DATABASE_URL, READONLY_DATABASE_URL


@contextmanager
def connect(readonly: bool = True) -> Iterator[psycopg.Connection]:
    """Open one connection. Rows come back as dictionaries."""
    url = READONLY_DATABASE_URL if readonly else DATABASE_URL
    with psycopg.connect(url, row_factory=dict_row) as conn:
        yield conn
