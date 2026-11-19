"""Compact schema summary that goes into the SQL-writing prompt.

For each table: row count, columns with declared types, primary key, foreign
keys, and a few sample values per column. Low-cardinality text columns list
every distinct value (that is where models most often guess wrong, e.g.
'UK' vs 'United Kingdom'); other columns show three examples.
"""
from __future__ import annotations

import sqlite3
from functools import lru_cache
from pathlib import Path

from .db import DEFAULT_DB

ALL_VALUES_UP_TO = 12   # list every value of a categorical text column with <= this many
SAMPLES = 3
MAX_SAMPLE_LEN = 40


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _fmt(v) -> str:
    if isinstance(v, str):
        s = v if len(v) <= MAX_SAMPLE_LEN else v[: MAX_SAMPLE_LEN - 3] + "..."
        return "'" + s.replace("'", "''") + "'"
    return repr(v)


def build_schema_summary(conn: sqlite3.Connection, samples: bool = True) -> str:
    """samples=False gives the bare schema (names, types, keys) used in the ablation run."""
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    lines = [f"SQLite database with {len(tables)} tables.", ""]
    for t in tables:
        n_rows = conn.execute(f"SELECT COUNT(*) FROM {_q(t)}").fetchone()[0]
        cols = conn.execute(f"PRAGMA table_info({_q(t)})").fetchall()
        fks = conn.execute(f"PRAGMA foreign_key_list({_q(t)})").fetchall()
        pk = [c[1] for c in sorted(cols, key=lambda c: c[5]) if c[5]]
        lines.append(f"TABLE {t} ({n_rows} rows)")
        for _cid, name, ctype, notnull, _default, _pk in cols:
            distinct = conn.execute(f"SELECT COUNT(DISTINCT {_q(name)}) FROM {_q(t)}").fetchone()[0]
            is_text = "CHAR" in (ctype or "").upper() or "TEXT" in (ctype or "").upper()
            if is_text and distinct <= ALL_VALUES_UP_TO and distinct < n_rows:
                vals = [r[0] for r in conn.execute(
                    f"SELECT DISTINCT {_q(name)} FROM {_q(t)} WHERE {_q(name)} IS NOT NULL ORDER BY 1")]
                sample = "all values: " + ", ".join(_fmt(v) for v in vals)
            else:
                vals = [r[0] for r in conn.execute(
                    f"SELECT {_q(name)} FROM {_q(t)} WHERE {_q(name)} IS NOT NULL "
                    f"GROUP BY {_q(name)} ORDER BY COUNT(*) DESC, {_q(name)} LIMIT {SAMPLES}")]
                sample = "e.g. " + ", ".join(_fmt(v) for v in vals)
            nulls = "" if notnull else " nullable"
            if samples:
                lines.append(f"  - {name} {ctype}{nulls}; {distinct} distinct; {sample}")
            else:
                lines.append(f"  - {name} {ctype}{nulls}")
        if pk:
            lines.append(f"  primary key: {', '.join(pk)}")
        for fk in fks:
            lines.append(f"  foreign key: {fk[3]} -> {fk[2]}.{fk[4]}")
        lines.append("")
    if samples:
        lines.append("Notes: dates are stored as TEXT 'YYYY-MM-DD HH:MM:SS'; use strftime() or substr() on them.")
    return "\n".join(lines).rstrip() + "\n"


@lru_cache(maxsize=4)
def schema_summary(path: str = str(DEFAULT_DB), samples: bool = True) -> str:
    # Plain read-only connection: PRAGMA table_info is blocked by the
    # authorizer used for model-written queries.
    conn = sqlite3.connect(f"{Path(path).as_uri()}?mode=ro", uri=True)
    try:
        return build_schema_summary(conn, samples=samples)
    finally:
        conn.close()
