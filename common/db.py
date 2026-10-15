"""Storage: PostgreSQL (pg8000, pure Python) or SQLite (CASCADE_DB=sqlite).

SQL is written once with `?` placeholders and portable `ON CONFLICT` upserts. The Postgres DDL in
deploy/init_db.sql is generated from DDL below (`python -m common.db --ddl postgres`) and a test
checks that the two never drift apart.
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import threading
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

_DDL = """
CREATE TABLE IF NOT EXISTS alerts (
    alert_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    pharmacy_id TEXT NOT NULL,
    score {REAL} NOT NULL,
    status TEXT NOT NULL,
    index_atc_l4 TEXT,
    marker_atc_l4 TEXT,
    pair_status TEXT,
    enc_blob {BLOB} NOT NULL,
    created_ts TEXT NOT NULL,
    updated_ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS alerts_pharmacy_idx ON alerts (pharmacy_id, created_ts);
CREATE TABLE IF NOT EXISTS latency (
    id {SERIAL},
    event_id TEXT NOT NULL,
    ms {REAL} NOT NULL,
    alerted {INT} NOT NULL,
    wall_ts {REAL} NOT NULL
);
CREATE INDEX IF NOT EXISTS latency_wall_idx ON latency (wall_ts);
CREATE TABLE IF NOT EXISTS signals (
    id {SERIAL},
    snapshot_ts TEXT NOT NULL,
    sim_day {INT} NOT NULL,
    a_l4 TEXT NOT NULL,
    b_l4 TEXT NOT NULL,
    win {INT} NOT NULL,
    n_ab {INT} NOT NULL,
    n_ba {INT} NOT NULL,
    nsr {REAL},
    asr {REAL},
    lo {REAL},
    hi {REAL},
    p_value {REAL},
    is_signal {INT} NOT NULL,
    is_signal_bh {INT} NOT NULL,
    status TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS signals_pair_idx ON signals (a_l4, b_l4, win, sim_day);
CREATE TABLE IF NOT EXISTS signals_latest (
    a_l4 TEXT NOT NULL,
    b_l4 TEXT NOT NULL,
    win {INT} NOT NULL,
    sim_day {INT} NOT NULL,
    n_ab {INT} NOT NULL,
    n_ba {INT} NOT NULL,
    nsr {REAL},
    asr {REAL},
    lo {REAL},
    hi {REAL},
    p_value {REAL},
    is_signal {INT} NOT NULL,
    is_signal_bh {INT} NOT NULL,
    status TEXT NOT NULL,
    first_signal_day {INT},
    updated_ts TEXT NOT NULL,
    PRIMARY KEY (a_l4, b_l4, win)
);
CREATE TABLE IF NOT EXISTS drafts (
    alert_id TEXT PRIMARY KEY,
    note_text {BLOB} NOT NULL,
    report_json {BLOB} NOT NULL,
    generator TEXT NOT NULL,
    validator_pass {INT} NOT NULL,
    reject_reasons TEXT NOT NULL,
    gen_ms {REAL} NOT NULL,
    created_ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    id {SERIAL},
    ts TEXT NOT NULL,
    role TEXT NOT NULL,
    action TEXT NOT NULL,
    object_id TEXT
);
CREATE TABLE IF NOT EXISTS feedback (
    id {SERIAL},
    alert_id TEXT NOT NULL,
    label TEXT NOT NULL,
    reason_enc {BLOB},
    ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS service_state (
    name TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_ts TEXT NOT NULL
);
"""

_TYPES = {
    "sqlite": {"REAL": "REAL", "BLOB": "BLOB", "INT": "INTEGER", "SERIAL": "INTEGER PRIMARY KEY AUTOINCREMENT"},
    "postgres": {"REAL": "DOUBLE PRECISION", "BLOB": "BYTEA", "INT": "BIGINT", "SERIAL": "BIGSERIAL PRIMARY KEY"},
}

_INSERT_VALUES = re.compile(r"^(INSERT INTO .+? VALUES )\((\?(?:, \?)*)\)(.*)$", re.S)

TABLES = ("alerts", "latency", "signals", "signals_latest", "drafts", "audit", "feedback", "service_state")


def ddl(dialect: str) -> str:
    return _DDL.format(**_TYPES[dialect]).strip() + "\n"


def ddl_statements(dialect: str) -> list[str]:
    return [s.strip() for s in ddl(dialect).split(";") if s.strip()]


class Database:
    def __init__(self, dialect: str, conn: Any) -> None:
        self.dialect = dialect
        self._conn = conn
        self._lock = threading.Lock()

    # ----- connection -----
    @classmethod
    def connect(cls, backend: str | None = None, url: str | None = None, sqlite_path: Path | None = None) -> Database:
        from common.config import get_settings

        s = get_settings()
        backend = backend or s.db_backend
        if backend == "sqlite":
            path = sqlite_path or s.sqlite_path
            if str(path) != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None, timeout=30)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            return cls("sqlite", conn)
        import pg8000.dbapi

        u = urlparse(url or s.database_url)
        conn = pg8000.dbapi.connect(
            user=unquote(u.username or "cascade"), password=unquote(u.password or "") or os.environ.get(
                "POSTGRES_PASSWORD") or None, host=u.hostname or "localhost", port=u.port or 5432,
            database=(u.path or "/cascade").lstrip("/") or "cascade")
        conn.autocommit = True
        return cls("postgres", conn)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _sql(self, sql: str) -> str:
        return sql if self.dialect == "sqlite" else re.sub(r"\?", "%s", sql)

    # ----- execution -----
    def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(self._sql(sql), tuple(params))
            cur.close()

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        rows = [tuple(r) for r in rows]
        if not rows:
            return
        with self._lock:
            cur = self._conn.cursor()
            if self.dialect == "sqlite":
                cur.execute("BEGIN")
                cur.executemany(sql, rows)
                cur.execute("COMMIT")
            else:
                m = _INSERT_VALUES.match(sql)
                if m is None:
                    cur.executemany(self._sql(sql), rows)
                else:  # one multi-row INSERT per chunk: far fewer round trips than executemany
                    head, group, tail = m.group(1), "(" + m.group(2).replace("?", "%s") + ")", m.group(3)
                    for i in range(0, len(rows), 500):
                        chunk = rows[i:i + 500]
                        cur.execute(head + ", ".join([group] * len(chunk)) + tail,
                                    tuple(v for r in chunk for v in r))
            cur.close()

    def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(self._sql(sql), tuple(params))
            rows = cur.fetchall()
            cur.close()
        return [tuple(r) for r in rows]

    def fetchone(self, sql: str, params: Sequence[Any] = ()) -> tuple[Any, ...] | None:
        rows = self.fetchall(sql, params)
        return rows[0] if rows else None

    def init_schema(self) -> None:
        for stmt in ddl_statements(self.dialect):
            self.execute(stmt)

    def truncate_all(self) -> None:
        for t in TABLES:
            self.execute(f"DELETE FROM {t}")

    # ----- small key/value for cross-service state -----
    def set_state(self, name: str, value: str, now: str) -> None:
        self.execute("INSERT INTO service_state (name, value, updated_ts) VALUES (?, ?, ?) "
                     "ON CONFLICT (name) DO UPDATE SET value = excluded.value, updated_ts = excluded.updated_ts",
                     (name, value, now))

    def get_state(self, name: str) -> str | None:
        row = self.fetchone("SELECT value FROM service_state WHERE name = ?", (name,))
        return row[0] if row else None


def main() -> None:
    ap = argparse.ArgumentParser(description="CASCADE database helper")
    ap.add_argument("--ddl", choices=["postgres", "sqlite"], help="print the DDL")
    ap.add_argument("--init", action="store_true", help="create tables in the configured database")
    args = ap.parse_args()
    if args.ddl:
        print("-- generated by `python -m common.db --ddl postgres`; do not edit by hand\n" + ddl(args.ddl), end="")
    if args.init:
        db = Database.connect()
        db.init_schema()
        print(f"schema ready ({db.dialect})")


if __name__ == "__main__":
    main()
