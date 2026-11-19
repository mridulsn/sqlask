"""Run the question set through the pipeline and score execution accuracy.

    uv run sqlask-eval                      # ClaudeCLIProvider, sonnet for SQL, haiku for answers
    uv run sqlask-eval --out web/data/runs.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .db import DEFAULT_DB, ROOT, open_readonly, run_query
from .disguise import DISGUISED_DB
from .guard import DEFAULT_MAX_ROWS, check_sql
from .pipeline import MAX_ATTEMPTS, ask
from .providers import (AnthropicProvider, ClaudeCLIProvider, FakeProvider, Provider,
                        resolve_model)
from .schema import schema_summary
from .scoring import results_match

QUESTIONS = ROOT / "eval" / "questions.json"
HARD_QUESTIONS = ROOT / "eval" / "hard_questions.json"
DIFFICULTIES = ("easy", "medium", "hard")
# question set -> (questions file, database). "disguised" is Chinook with renamed
# tables/columns, shifted dates, scaled prices and replaced values (disguise.py).
SUITES = {
    "chinook": (QUESTIONS, DEFAULT_DB),
    "disguised": (HARD_QUESTIONS, DISGUISED_DB),
}


def load_questions(path: Path = QUESTIONS) -> list[dict]:
    return json.loads(Path(path).read_text())


def gold_result(conn, q: dict):
    return run_query(conn, check_sql(q["gold_sql"]).sql)


def evaluate_one(q: dict, provider: Provider, db_path: Path, schema: str,
                 sql_model: str, answer_model: str, write_answer) -> dict:
    conn = open_readonly(db_path)  # one connection per worker thread
    try:
        gold = gold_result(conn, q) if q.get("gold_sql") else None
        res = ask(q["question"], provider, conn, schema, sql_model=sql_model,
                  answer_model=answer_model, write_answer=write_answer)
    finally:
        conn.close()
    if gold is None:  # demo prompt without a gold query: recorded, not scored
        correct = strict = None
    else:
        correct = bool(res.ok and results_match(gold.rows, res.rows, ordered=q.get("ordered", False)))
        strict = bool(res.ok and results_match(gold.rows, res.rows, ordered=q.get("ordered", False),
                                               strict=True))
    attempts = [{
        "n": a.n, "sql": a.sql, "error": a.error, "error_kind": a.error_kind,
        "row_count": a.row_count, "model": a.model, "cost_usd": a.cost_usd,
        "latency_ms": a.latency_ms,
    } for a in res.attempts]
    cost = sum(a.cost_usd or 0 for a in res.attempts) + (res.answer_meta.get("cost_usd") or 0)
    return {
        "id": q["id"], "difficulty": q["difficulty"], "type": q.get("type"),
        "tags": q.get("tags", []),
        "question": q["question"], "ordered": q.get("ordered", False),
        "gold_sql": q.get("gold_sql"), "gold_row_count": len(gold.rows) if gold else None,
        "note": q.get("note"),
        "final_sql": res.sql, "ok": res.ok, "row_count": len(res.rows),
        "answer": res.answer, "answer_model": res.answer_meta.get("model"),
        "attempts": attempts, "n_attempts": len(res.attempts),
        "correct": correct, "correct_strict": strict,
        "cost_usd": round(cost, 6),
    }


def summarize(all_runs: list[dict]) -> dict:
    runs = [r for r in all_runs if r["correct"] is not None]  # scored runs only
    def acc(rs, key="correct"):
        return {"n": len(rs), "correct": sum(r[key] for r in rs),
                "accuracy": round(sum(r[key] for r in rs) / len(rs), 3) if rs else None}

    retried = [r for r in runs if r["n_attempts"] > 1]
    kinds: dict[str, int] = {}
    for r in runs:
        for a in r["attempts"]:
            if a["error_kind"]:
                kinds[a["error_kind"]] = kinds.get(a["error_kind"], 0) + 1
    first_try = [r for r in runs if r["n_attempts"] == 1]
    types = list(dict.fromkeys(r.get("type") for r in runs if r.get("type")))
    return {
        "overall": acc(runs),
        "overall_strict": acc(runs, "correct_strict"),
        "by_difficulty": {d: acc([r for r in runs if r["difficulty"] == d]) for d in DIFFICULTIES
                          if any(r["difficulty"] == d for r in runs)},
        "by_type": {t: acc([r for r in runs if r.get("type") == t]) for t in types},
        "first_attempt_ok": len(first_try),
        "needed_retry": len(retried),
        "retry_then_correct": sum(r["correct"] for r in retried),
        # wrong on the first try with no error or empty result to trigger a retry
        "wrong_without_retry": sum(1 for r in first_try if not r["correct"]),
        "retry_ids": [r["id"] for r in retried],
        "failed_ids": [r["id"] for r in runs if not r["correct"]],
        "error_kinds": kinds,
        "sql_calls": sum(r["n_attempts"] for r in all_runs),
        "total_cost_usd": round(sum(r["cost_usd"] for r in all_runs), 4),
        "unscored": len(all_runs) - len(runs),
    }


def write_set(path: Path, entry: dict) -> None:
    """runs.json holds several run sets; replace the one with this name."""
    data = {"sets": []}
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError:
            pass
        if "sets" not in data:
            data = {"sets": []}
    data["sets"] = [s for s in data["sets"] if s["name"] != entry["name"]] + [entry]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")


def _cli_version() -> str | None:
    try:
        out = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=30)
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def make_provider(name: str, questions: list[dict] | None = None) -> Provider:
    if name == "anthropic":
        return AnthropicProvider()
    if name == "cli":
        return ClaudeCLIProvider(cache_dir=ROOT / ".cache" / "claude-cli")
    if name == "fake":  # smoke test: always answers with the gold SQL
        gold = {q["question"]: q["gold_sql"] for q in (questions or load_questions())
                if q.get("gold_sql")}

        def reply(system, prompt, model):
            for question, sql in gold.items():
                if f"Question: {question}" in prompt and "SQL that was run" not in prompt:
                    return f"```sql\n{sql}\n```"
            return "Fake answer."
        return FakeProvider(reply)
    raise SystemExit(f"unknown provider {name}")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Score sqlask on the gold question set")
    p.add_argument("--provider", choices=["cli", "anthropic", "fake"], default="cli")
    p.add_argument("--sql-model", default="sonnet")
    p.add_argument("--answer-model", default="haiku")
    p.add_argument("--answers", choices=["all", "retried", "none"], default="all",
                   help="which questions get the answer-writing call (fewer calls = cheaper)")
    p.add_argument("--only", nargs="*", help="question ids to run")
    p.add_argument("--suite", choices=sorted(SUITES), default="chinook",
                   help="question set + database: chinook (40 questions) or disguised (20 harder)")
    p.add_argument("--questions", default=None, help="question file (overrides --suite)")
    p.add_argument("--parallel", type=int, default=3)
    p.add_argument("--db", default=None, help="database file (overrides --suite)")
    p.add_argument("--schema", choices=["full", "bare"], default="full",
                   help="bare = no sample values or notes (ablation)")
    p.add_argument("--set-name", default="main", help="name of this run set inside the output file")
    p.add_argument("--label", default=None, help="human label for this run set")
    p.add_argument("--out", default=str(ROOT / "web" / "data" / "runs.json"))
    args = p.parse_args(argv)

    suite_questions, suite_db = SUITES[args.suite]
    qs = load_questions(Path(args.questions or suite_questions))
    if args.only:
        qs = [q for q in qs if q["id"] in set(args.only)]
    provider = make_provider(args.provider, qs)
    db_path = Path(args.db or suite_db)
    schema = schema_summary(str(db_path), samples=args.schema == "full")

    with ThreadPoolExecutor(max_workers=max(1, min(args.parallel, 3))) as pool:
        futures = [pool.submit(evaluate_one, q, provider, db_path, schema, args.sql_model,
                               args.answer_model,
                               {"all": True, "none": False, "retried": "retried"}[args.answers])
                   for q in qs]
        runs = []
        for q, f in zip(qs, futures):
            r = f.result()
            runs.append(r)
            mark = {True: "ok  ", False: "FAIL", None: "rec "}[r["correct"]]
            print(f"{mark} {r['id']} attempts={r['n_attempts']} {r['question']}", flush=True)

    summary = summarize(runs)
    provider_label = {"cli": "ClaudeCLIProvider (Claude Code CLI, headless `claude -p`)",
                      "anthropic": "AnthropicProvider (anthropic Python SDK)",
                      "fake": "FakeProvider (gold SQL echo, smoke test only)"}[args.provider]
    entry = {
        "name": args.set_name,
        "label": args.label or f"{resolve_model(args.sql_model)}, {args.schema} schema",
        "meta": {
            "recorded": dt.date.today().isoformat(),
            "provider": provider_label,
            "cli_version": _cli_version() if args.provider == "cli" else None,
            "sql_model": resolve_model(args.sql_model),
            "answer_model": None if args.answers == "none" else resolve_model(args.answer_model),
            "answers_written_for": args.answers,
            "max_attempts": MAX_ATTEMPTS,
            "max_rows": DEFAULT_MAX_ROWS,
            "database": db_path.name,
            "suite": args.suite,
            "n_questions": len(runs),
            "schema": args.schema,
        },
        "summary": summary,
        "runs": runs,
    }
    write_set(Path(args.out), entry)
    s = summary
    if s["overall"]["n"]:
        print(f"\nexecution accuracy {s['overall']['correct']}/{s['overall']['n']} "
              f"({s['overall']['accuracy']:.1%}); strict {s['overall_strict']['accuracy']:.1%}")
        for d, v in s["by_difficulty"].items():
            print(f"  {d:6s} {v['correct']}/{v['n']}")
        for t, v in s["by_type"].items():
            print(f"  {t:12s} {v['correct']}/{v['n']}")
    print(f"needed retry: {s['needed_retry']}, of which correct: {s['retry_then_correct']}; "
          f"wrong with no retry: {s['wrong_without_retry']}")
    print(f"cost: ${s['total_cost_usd']}  -> {args.out}")


if __name__ == "__main__":
    main()
