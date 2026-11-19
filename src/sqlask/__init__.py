"""sqlask: ask a SQLite database questions in plain English."""
from __future__ import annotations

import argparse
import json
import sys

from .db import DEFAULT_DB, open_readonly
from .pipeline import ask
from .providers import AnthropicProvider, ClaudeCLIProvider, default_provider
from .schema import schema_summary

__all__ = ["ask", "open_readonly", "schema_summary", "main"]


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="sqlask", description="Ask the Chinook database a question")
    p.add_argument("question")
    p.add_argument("--provider", choices=["auto", "cli", "anthropic"], default="auto",
                   help="auto = Anthropic SDK if ANTHROPIC_API_KEY is set, else the Claude Code CLI")
    p.add_argument("--sql-model", default="sonnet")
    p.add_argument("--answer-model", default="haiku")
    p.add_argument("--db", default=str(DEFAULT_DB))
    p.add_argument("--json", action="store_true", help="print the full result as JSON")
    args = p.parse_args(argv)

    provider = {"auto": default_provider, "cli": ClaudeCLIProvider,
                "anthropic": AnthropicProvider}[args.provider]()
    conn = open_readonly(args.db)
    res = ask(args.question, provider, conn, schema_summary(args.db),
              sql_model=args.sql_model, answer_model=args.answer_model)
    if args.json:
        json.dump(res.to_dict(), sys.stdout, indent=1, ensure_ascii=False, default=str)
        print()
        return
    for a in res.attempts:
        status = a.error or f"ok, {a.row_count} rows"
        print(f"[attempt {a.n}] {status}")
    print(f"\nSQL:\n{res.sql}\n")
    print(" | ".join(res.columns))
    for r in res.rows[:20]:
        print(" | ".join("" if v is None else str(v) for v in r))
    if len(res.rows) > 20:
        print(f"... {len(res.rows) - 20} more rows")
    print(f"\nAnswer: {res.answer}")
