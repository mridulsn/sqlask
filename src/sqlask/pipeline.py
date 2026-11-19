"""Question -> SQL -> guarded execution -> retry on failure -> plain-English answer."""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from decimal import Decimal

from .db import QueryResult, QueryTimeout, run_query
from .guard import DEFAULT_MAX_ROWS, GuardError, check_sql
from .providers import Provider, ProviderError

MAX_ATTEMPTS = 3
ANSWER_ROWS = 50  # rows shown to the answer model

SQL_SYSTEM = """You write SQLite queries that answer questions about one database.

Rules:
- Write exactly ONE read-only SQLite statement: SELECT, or WITH ... SELECT.
- Use only tables and columns that appear in the schema. SQLite dialect only
  (strftime/substr for dates, || for string concatenation, no ILIKE, no DATE_TRUNC).
- Return only the columns the question asks for, plus a name column when the
  answer is about people or things. Keep ties unless the question asks for exactly N.
- Text filters must match stored values exactly; check the sample values.
- Reply with the query in a single ```sql fenced block and nothing else."""

ANSWER_SYSTEM = """You turn SQL query results into a short answer for a non-technical reader.

Rules:
- Answer the question in 1-3 plain sentences using only the result rows given.
- Mention the key numbers or names. Do not describe the SQL.
- If there are no rows, say that no matching records were found.
- If the rows were cut off, say the list is partial."""


@dataclass
class Attempt:
    n: int
    sql: str | None
    executed_sql: str | None = None
    error: str | None = None
    error_kind: str | None = None   # parse | guard | sql_error | timeout | empty | provider
    row_count: int | None = None
    columns: list[str] | None = None
    model: str | None = None
    cost_usd: float | None = None
    latency_ms: float | None = None
    raw: str | None = None


@dataclass
class AskResult:
    question: str
    answer: str
    sql: str | None
    columns: list[str]
    rows: list[list]
    ok: bool
    truncated: bool = False
    attempts: list[Attempt] = field(default_factory=list)
    answer_meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def extract_sql(text: str) -> str | None:
    """Pull the query out of a model reply (fenced block preferred)."""
    if not text:
        return None
    m = re.search(r"```(?:sql|sqlite)?\s*\n?(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    sql = m.group(1) if m else text
    sql = sql.strip()
    if not m:
        start = re.search(r"\b(SELECT|WITH)\b", sql, flags=re.IGNORECASE)
        if not start:
            return None
        sql = sql[start.start():].strip()
    return sql or None


def _norm_sql(sql: str | None) -> str:
    return re.sub(r"\s+", " ", (sql or "").strip().rstrip(";")).lower()


def is_suspicious_empty(res: QueryResult) -> bool:
    """No rows, or a single row whose values are all NULL/0 (e.g. COUNT=0,
    SUM=NULL). Both usually mean a filter value did not match the data."""
    if not res.rows:
        return True
    if len(res.rows) == 1:
        return all(v is None or (isinstance(v, (int, float)) and v == 0) for v in res.rows[0])
    return False


def build_sql_prompt(schema: str, question: str, history: list[Attempt]) -> str:
    parts = ["Database schema:", schema, f"Question: {question}"]
    if history:
        parts.append("\nYour earlier attempts did not work:")
        for a in history:
            parts.append(f"\nAttempt {a.n}:\n```sql\n{a.sql or '(no SQL found)'}\n```\nProblem: {a.error}")
        last = history[-1]
        if last.error_kind == "empty":
            parts.append("\nFix the query. If you are sure the empty result is the true answer, "
                         "return the same query unchanged.")
        else:
            parts.append("\nWrite a corrected query.")
    return "\n".join(parts)


def _jsonable(v):
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, bytes):
        return f"<{len(v)} bytes>"
    return v


def build_answer_prompt(question: str, sql: str, res: QueryResult) -> str:
    rows = [[_jsonable(v) for v in r] for r in res.rows[:ANSWER_ROWS]]
    note = ""
    if res.truncated or len(res.rows) > ANSWER_ROWS:
        note = f"\n(Only the first {len(rows)} rows are shown; the full result is longer.)"
    return (f"Question: {question}\n\nSQL that was run:\n{sql}\n\n"
            f"Columns: {json.dumps(res.columns)}\nRows ({len(res.rows)}):\n"
            + "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + note)


def ask(question: str, provider: Provider, conn: sqlite3.Connection, schema: str,
        sql_model: str = "sonnet", answer_model: str = "haiku",
        max_attempts: int = MAX_ATTEMPTS, max_rows: int = DEFAULT_MAX_ROWS,
        write_answer: bool = True) -> AskResult:
    attempts: list[Attempt] = []
    final: QueryResult | None = None
    final_sql: str | None = None

    for n in range(1, max_attempts + 1):
        prompt = build_sql_prompt(schema, question, attempts)
        try:
            comp = provider.complete(SQL_SYSTEM, prompt, model=sql_model)
        except ProviderError as e:
            attempts.append(Attempt(n=n, sql=None, error=str(e), error_kind="provider"))
            break  # the model is unavailable; retrying the same call will not help
        att = Attempt(n=n, sql=extract_sql(comp.text), model=comp.model,
                      cost_usd=comp.cost_usd, latency_ms=comp.latency_ms, raw=comp.text)
        attempts.append(att)
        if att.sql is None:
            att.error, att.error_kind = "the reply did not contain a SQL query", "parse"
            continue
        try:
            g = check_sql(att.sql, max_rows=max_rows)
        except GuardError as e:
            att.error, att.error_kind = f"rejected by read-only guard: {e}", "guard"
            continue
        att.executed_sql = g.sql
        try:
            res = run_query(conn, g.sql, max_rows=max_rows)
        except QueryTimeout as e:
            att.error, att.error_kind = str(e), "timeout"
            continue
        except sqlite3.DatabaseError as e:
            msg = str(e)
            if "not authorized" in msg:
                msg += " (only reading is allowed)"
            att.error, att.error_kind = f"SQLite error: {msg}", "sql_error"
            continue
        # the guard's LIMIT stops SQLite at max_rows, so hitting it exactly
        # means the real result was probably longer
        if (g.limit_added or g.limit_capped) and len(res.rows) >= max_rows:
            res.truncated = True
        att.row_count, att.columns = len(res.rows), res.columns
        final, final_sql = res, att.sql

        confirmed = len(attempts) >= 2 and attempts[-2].error_kind == "empty" \
            and _norm_sql(attempts[-2].sql) == _norm_sql(att.sql)
        if is_suspicious_empty(res) and not confirmed and n < max_attempts:
            what = "no rows" if not res.rows else "a single row of NULL/0 values"
            att.error = (f"the query ran but returned {what}. This often means a filter value does "
                         "not match how the data is stored (spelling, case, abbreviation, date "
                         "format) or a join drops every row.")
            att.error_kind = "empty"
            continue
        break

    if final is None:
        last = attempts[-1] if attempts else None
        return AskResult(question=question,
                         answer=f"Could not produce a working query after {len(attempts)} attempt(s). "
                                f"Last problem: {last.error if last else 'unknown'}",
                         sql=last.sql if last else None, columns=[], rows=[], ok=False,
                         attempts=attempts)

    rows = [[_jsonable(v) for v in r] for r in final.rows]
    answer, meta = "", {}
    if write_answer:
        try:
            comp = provider.complete(ANSWER_SYSTEM, build_answer_prompt(question, final_sql, final),
                                     model=answer_model, max_tokens=1000)
            answer = comp.text.strip()
            meta = {"model": comp.model, "cost_usd": comp.cost_usd, "latency_ms": comp.latency_ms}
        except ProviderError as e:
            answer = f"(answer step failed: {e})"
    return AskResult(question=question, answer=answer, sql=final_sql, columns=final.columns,
                     rows=rows, ok=True, truncated=final.truncated, attempts=attempts,
                     answer_meta=meta)
