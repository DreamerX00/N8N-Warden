"""Database access, uniform across SQLite and Postgres.

Both backends expose the same four methods, so every caller above this layer
is dialect-agnostic. Timestamps are generated in Python rather than SQL
precisely so that no caller has to know which dialect it is talking to.
"""

from __future__ import annotations

import secrets
import sqlite3
from datetime import datetime
from pathlib import Path

from .config import NANOID_ALPHABET
from .errors import Fatal


def now_ts() -> str:
    """n8n's datetime(3) format, e.g. 2026-06-03 05:51:19.476."""
    now = datetime.now()
    return now.strftime("%Y-%m-%d %H:%M:%S.") + f"{now.microsecond // 1000:03d}"


def nanoid(length: int = 16) -> str:
    """n8n generates 16-character nanoids for project, workflow and folder ids."""
    return "".join(secrets.choice(NANOID_ALPHABET) for _ in range(length))


class Db:
    """SQLite. Foreign keys are switched ON — SQLite ships them off, which
    would leave even the constraints n8n does declare completely inert."""

    dialect = "sqlite"

    def __init__(self, path: Path, readonly: bool = False):
        # Read-only matters when opening the live file in place: it guarantees
        # a stray write cannot touch a database n8n has open.
        if readonly:
            self.conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        else:
            self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        try:
            # First actual read of the file — a garbage/encrypted/corrupt file
            # fails here with a message instead of on some later query.
            self.conn.execute("SELECT 1 FROM sqlite_master LIMIT 1")
        except sqlite3.DatabaseError:
            raise Fatal(f"{path} is not a SQLite database")
        self.conn.execute("PRAGMA foreign_keys=ON")

    def q(self, sql: str, *params) -> list:
        return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, *params):
        return self.conn.execute(sql, params).fetchone()

    def exec(self, sql: str, *params):
        return self.conn.execute(sql, params)

    def tables(self) -> set[str]:
        return {r[0] for r in self.q("SELECT name FROM sqlite_master WHERE type='table'")}

    def columns(self, table: str) -> set[str]:
        return {r[1] for r in self.q(f'PRAGMA table_info("{table}")')}


class PgDb(Db):
    """Postgres via psycopg2.

    Parameter binding is delegated to the driver rather than hand-rolled — an
    admin tool that interpolates operator-supplied names into SQL is a bug
    waiting to happen.
    """

    dialect = "postgres"

    def __init__(self, inst):
        try:
            import psycopg2
            import psycopg2.extras
        except ImportError:
            raise Fatal("this n8n uses Postgres; install a driver first:\n"
                        "      pip install psycopg2-binary")
        settings = {k: v for k, v in inst.pg.items() if k != "schema"}
        self.conn = psycopg2.connect(**settings)
        self._cur = self.conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    @staticmethod
    def _tr(sql: str) -> str:
        return sql.replace("?", "%s")

    def q(self, sql, *params):
        self._cur.execute(self._tr(sql), params)
        return self._cur.fetchall()

    def one(self, sql, *params):
        self._cur.execute(self._tr(sql), params)
        return self._cur.fetchone()

    def exec(self, sql, *params):
        self._cur.execute(self._tr(sql), params)
        return self._cur

    def tables(self):
        return {r[0] for r in self.q(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='public'")}

    def columns(self, table):
        return {r[0] for r in self.q(
            "SELECT column_name FROM information_schema.columns WHERE table_name=?", table)}
