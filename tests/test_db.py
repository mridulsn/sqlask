import sqlite3

import pytest

from sqlask.db import QueryTimeout, open_readonly, run_query


@pytest.fixture()
def conn():
    c = open_readonly()
    yield c
    c.close()


@pytest.mark.parametrize("sql", [
    "DELETE FROM Track",
    "UPDATE Track SET Name = 'x'",
    "INSERT INTO Genre VALUES (999, 'x')",
    "DROP TABLE Track",
    "CREATE TABLE x (a)",
    "PRAGMA query_only = OFF",
])
def test_writes_blocked_even_without_guard(conn, sql):
    # layer 2/3: read-only file + authorizer, independent of the text guard
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute(sql)
    assert conn.execute("SELECT COUNT(*) FROM Track").fetchone()[0] == 3503


def test_row_cap_and_truncation_flag(conn):
    res = run_query(conn, "SELECT TrackId FROM Track", max_rows=10)
    assert len(res.rows) == 10 and res.truncated
    assert res.columns == ["TrackId"]


def test_timeout(conn):
    slow = "SELECT COUNT(*) FROM PlaylistTrack a, PlaylistTrack b"
    with pytest.raises(QueryTimeout):
        run_query(conn, slow, timeout_s=0.2)
    # connection still usable afterwards
    assert run_query(conn, "SELECT 1").rows == [(1,)]
