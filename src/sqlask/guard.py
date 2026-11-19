"""Read-only guard for model-written SQL.

The guard is the first of three layers (guard -> read-only connection ->
SQLite authorizer, see db.py). It only lets through one SELECT (or WITH ...
SELECT) statement and makes sure the outermost query has a LIMIT.

It works on a "masked" copy of the SQL where string literals, quoted
identifiers and comments are blanked out, so a ';' or 'DROP' inside a string
cannot fool it. The same algorithm is ported to JavaScript in web/app.js and
both are checked against tests/guard_cases.json.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

DEFAULT_MAX_ROWS = 200

# Statement keywords that must never appear outside literals. REPLACE is
# handled separately because replace() is also a harmless string function.
FORBIDDEN = (
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "ATTACH",
    "DETACH", "PRAGMA", "VACUUM", "REINDEX", "ANALYZE", "BEGIN", "COMMIT",
    "ROLLBACK", "SAVEPOINT", "RELEASE", "TRUNCATE", "GRANT", "LOAD_EXTENSION",
)


class GuardError(ValueError):
    """The SQL was rejected before it reached the database."""


@dataclass(frozen=True)
class GuardResult:
    sql: str          # the SQL that will actually run (LIMIT added or capped)
    limit_added: bool
    limit_capped: bool


def mask_sql(sql: str) -> str:
    """Return sql with literals, quoted identifiers and comments replaced by
    spaces (same length, so positions line up with the original).

    Raises GuardError on an unterminated string, identifier or comment."""
    out = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch in ("'", '"', "`", "["):
            close = "]" if ch == "[" else ch
            j = i + 1
            while True:
                if j >= n:
                    raise GuardError("unterminated quoted string or identifier")
                if sql[j] == close:
                    # doubled quote ('' or "") is an escaped quote, keep going
                    if close != "]" and j + 1 < n and sql[j + 1] == close:
                        j += 2
                        continue
                    break
                j += 1
            # keep quote chars so tokens stay separated; blank the inside
            out.append(ch + " " * (j - i - 1) + close)
            i = j + 1
        elif ch == "-" and sql.startswith("--", i):
            j = sql.find("\n", i)
            j = n if j == -1 else j
            out.append(" " * (j - i))
            i = j
        elif ch == "/" and sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            if j == -1:
                raise GuardError("unterminated /* comment")
            out.append(" " * (j + 2 - i))
            i = j + 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _top_level_limit(masked: str) -> re.Match | None:
    """Find a LIMIT keyword at parenthesis depth 0 (the outermost query)."""
    depth = 0
    last = None
    for m in re.finditer(r"\(|\)|\bLIMIT\b", masked, flags=re.IGNORECASE):
        tok = m.group(0)
        if tok == "(":
            depth += 1
        elif tok == ")":
            depth -= 1
        elif depth == 0:
            last = m
    return last


def check_sql(sql: str, max_rows: int = DEFAULT_MAX_ROWS) -> GuardResult:
    """Validate one read-only query and return the SQL to execute.

    Raises GuardError with a short reason the model can act on."""
    if sql is None or not sql.strip():
        raise GuardError("empty query")
    masked = mask_sql(sql)

    # Drop trailing semicolons/whitespace; any other ';' means 2+ statements.
    stripped = masked.rstrip()
    while stripped.endswith(";"):
        stripped = stripped[:-1].rstrip()
    if ";" in stripped:
        raise GuardError("only one statement is allowed")
    body = sql[: len(stripped)]
    masked = stripped

    first = re.match(r"\s*([A-Za-z]+)", masked)
    if not first or first.group(1).upper() not in ("SELECT", "WITH"):
        raise GuardError("only SELECT queries are allowed (must start with SELECT or WITH)")

    upper = masked.upper()
    for kw in FORBIDDEN:
        if re.search(rf"\b{kw}\b", upper):
            raise GuardError(f"forbidden keyword: {kw}")
    if re.search(r"\bREPLACE\s+INTO\b", upper):
        raise GuardError("forbidden keyword: REPLACE")
    if masked.count("(") != masked.count(")"):
        raise GuardError("unbalanced parentheses")

    limit = _top_level_limit(masked)
    if limit is None:
        return GuardResult(sql=f"{body}\nLIMIT {max_rows}", limit_added=True, limit_capped=False)

    # LIMIT n [OFFSET m] or LIMIT m, n: cap a plain integer that is too large.
    tail = masked[limit.end():]
    num = re.match(r"\s*(\d+)\s*(?:$|OFFSET\b)", tail, flags=re.IGNORECASE)
    if num:
        value = int(num.group(1))
        if value <= max_rows:
            return GuardResult(sql=body, limit_added=False, limit_capped=False)
        start = limit.end() + num.start(1)
        end = limit.end() + num.end(1)
        return GuardResult(sql=body[:start] + str(max_rows) + body[end:], limit_added=False, limit_capped=True)
    # Anything fancier (expressions, "LIMIT m, n"): wrap it so the cap holds.
    return GuardResult(sql=f"SELECT * FROM (\n{body}\n) LIMIT {max_rows}", limit_added=True, limit_capped=False)
