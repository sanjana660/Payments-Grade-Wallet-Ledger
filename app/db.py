import os
from contextlib import contextmanager
from psycopg import Connection
from psycopg.rows import dict_row

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://wallet:wallet@localhost:5432/wallet")

@contextmanager
def connection():
    with Connection.connect(DATABASE_URL, row_factory=dict_row) as conn:
        yield conn

@contextmanager
def transaction(isolation_level=None):
    with connection() as conn:
        if isolation_level:
            conn.execute(f"SET TRANSACTION ISOLATION LEVEL {isolation_level}")
        with conn.transaction():
            yield conn
