"""Read-only SQLite access.

Layers 2 and 3 of the read-only design:
  2. the file is opened with mode=ro and PRAGMA query_only=ON;
  3. an authorizer callback denies every action except reading, selecting,
     calling functions and recursive CTEs.
A progress handler aborts queries that run longer than the time budget.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = ROOT / "web" / "data" / "chinook.sqlite"

_ALLOWED_ACTIONS = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    getattr(sqlite3, "SQLITE_RECURSIVE", 33),
}


def _authorizer(action, arg1, arg2, dbname, source):
    return sqlite3.SQLITE_OK if action in _ALLOWED_ACTIONS else sqlite3.SQLITE_DENY


def open_readonly(path: str | Path = DEFAULT_DB) -> sqlite3.Connection:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, check_same_thread=False)
    conn.execute("PRAGMA query_only = ON")
    conn.set_authorizer(_authorizer)
    return conn


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[tuple]
    truncated: bool = False
    ms: float = 0.0
    extra: dict = field(default_factory=dict)


class QueryTimeout(RuntimeError):
    pass


def run_query(conn: sqlite3.Connection, sql: str, max_rows: int = 200,
              timeout_s: float = 5.0) -> QueryResult:
    """Run one already-guarded SELECT. Raises sqlite3.Error or QueryTimeout."""
    deadline = time.monotonic() + timeout_s
    timed_out = {"hit": False}

    def progress():
        if time.monotonic() > deadline:
            timed_out["hit"] = True
            return 1  # non-zero aborts the statement
        return 0

    conn.set_progress_handler(progress, 10_000)
    t0 = time.perf_counter()
    try:
        cur = conn.execute(sql)
        rows = cur.fetchmany(max_rows + 1)
    except sqlite3.OperationalError as e:
        if timed_out["hit"]:
            raise QueryTimeout(f"query exceeded {timeout_s:.0f}s time limit") from e
        raise
    finally:
        conn.set_progress_handler(None, 0)
    cols = [d[0] for d in (cur.description or [])]
    truncated = len(rows) > max_rows
    return QueryResult(columns=cols, rows=rows[:max_rows], truncated=truncated,
                       ms=(time.perf_counter() - t0) * 1000)
