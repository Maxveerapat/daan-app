"""Database access for GlubDD.

Production uses PostgreSQL via DATABASE_URL (e.g. a Neon or Supabase connection string).
Without DATABASE_URL the app falls back to a local SQLite file, for development.

Queries are written once with `?` placeholders; the Postgres adapter rewrites them to `%s`.
Schema changes go in MIGRATIONS (append-only, applied in order, tracked in schema_migrations).
"""
import os
import sqlite3
from pathlib import Path

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
IS_PG = DATABASE_URL.startswith(("postgres://", "postgresql://"))
SQLITE_PATH = Path(__file__).parent / "daan.db"

if IS_PG:
    import psycopg
    from psycopg.rows import dict_row


class DB:
    """Thin wrapper so the app code is identical on SQLite and Postgres."""

    def __init__(self):
        if IS_PG:
            self.conn = psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=10)
        else:
            self.conn = sqlite3.connect(SQLITE_PATH)
            self.conn.row_factory = sqlite3.Row

    def _sql(self, sql):
        return sql.replace("?", "%s") if IS_PG else sql

    def execute(self, sql, params=()):
        cur = self.conn.cursor()
        cur.execute(self._sql(sql), params)
        return cur

    def insert(self, sql, params=()):
        """Run an INSERT and return the new row's id."""
        if IS_PG:
            return self.execute(sql + " RETURNING id", params).fetchone()["id"]
        return self.execute(sql, params).lastrowid

    def scalar(self, sql, params=()):
        row = self.execute(sql, params).fetchone()
        if row is None:
            return None
        return list(row.values())[0] if IS_PG else row[0]

    def commit(self):
        self.conn.commit()

    def close(self):
        self.conn.close()


# Dialect-specific bits used in migrations.
PK = "BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY" if IS_PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
REAL = "DOUBLE PRECISION" if IS_PG else "REAL"

# Append-only. Never edit a migration that has shipped; add a new one.
MIGRATIONS = [
    # 1: reports (checkpoints + road incidents share one table; category comes from `type`)
    f"""CREATE TABLE IF NOT EXISTS checkpoints (
        id {PK},
        lat {REAL} NOT NULL,
        lng {REAL} NOT NULL,
        label TEXT NOT NULL,
        type TEXT NOT NULL,
        reported_at BIGINT NOT NULL,
        last_confirmed_at BIGINT NOT NULL,
        confirms INTEGER NOT NULL DEFAULT 1,
        dismisses INTEGER NOT NULL DEFAULT 0,
        reports INTEGER NOT NULL DEFAULT 1,
        cleared_at BIGINT,
        source TEXT NOT NULL DEFAULT 'user',
        source_name TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_checkpoints_last_confirmed ON checkpoints (last_confirmed_at)",
    # 3: who reported (anonymous device id now; user_id once accounts exist)
    "ALTER TABLE checkpoints ADD COLUMN reporter_device TEXT",
    "ALTER TABLE checkpoints ADD COLUMN reporter_user_id BIGINT",
    # 5: one row per vote, so a device can't confirm/dismiss the same report repeatedly
    f"""CREATE TABLE IF NOT EXISTS votes (
        id {PK},
        report_id BIGINT NOT NULL REFERENCES checkpoints(id) ON DELETE CASCADE,
        device_id TEXT NOT NULL,
        user_id BIGINT,
        still_there BOOLEAN NOT NULL,
        created_at BIGINT NOT NULL
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_votes_report_device ON votes (report_id, device_id)",
]


def _column_exists(db, table, column):
    if IS_PG:
        return db.scalar(
            "SELECT COUNT(*) FROM information_schema.columns WHERE table_name = ? AND column_name = ?",
            (table, column),
        ) > 0
    return any(r[1] == column for r in db.execute(f"PRAGMA table_info({table})").fetchall())


def migrate():
    db = DB()
    db.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY)")
    done = {r["version"] for r in db.execute("SELECT version FROM schema_migrations").fetchall()}
    for i, sql in enumerate(MIGRATIONS, start=1):
        if i in done:
            continue
        # Databases created before migrations existed may already have these columns.
        if sql.startswith("ALTER TABLE") and "ADD COLUMN" in sql:
            table, col = sql.split()[2], sql.split("ADD COLUMN")[1].split()[0]
            if _column_exists(db, table, col):
                db.execute("INSERT INTO schema_migrations (version) VALUES (?)", (i,))
                continue
        db.execute(sql)
        db.execute("INSERT INTO schema_migrations (version) VALUES (?)", (i,))
    db.commit()
    return db
