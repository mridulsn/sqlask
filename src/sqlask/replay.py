"""Recompute recorded runs with the current pipeline and scorer, no model calls.

    uv run sqlask-rescore                    # rewrites web/data/runs.json in place
    uv run sqlask-rescore --check            # only report what would change

Each recorded run keeps the model replies it got (the SQL of every attempt,
and the answer sentence). ReplayProvider hands those replies back in order,
so `ask()` re-makes every decision (guard, execution, suspicious-empty check,
confirmation, acceptance) and the scorer re-checks the result. If the current
pipeline would need a reply that was never recorded (for example a third
attempt where the old code stopped at two), replay stops with ReplayExhausted
and that question has to be re-run against the model.

Recorded answers were written from an earlier answer prompt that showed the
model's SQL rather than the executed SQL; they are reused as recorded.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

from .db import ROOT
from .evaluate import evaluate_one, summarize
from .pipeline import ANSWER_SYSTEM, SQL_SYSTEM
from .providers import Completion, ProviderError, resolve_model
from .schema import schema_summary

RUNS = ROOT / "web" / "data" / "runs.json"


class ReplayExhausted(RuntimeError):
    """The pipeline asked for a model reply that the recording does not have."""


class ReplayProvider:
    name = "replay"

    def __init__(self, run: dict):
        self.run = run
        self.sql_replies = list(run["attempts"])
        sql_cost = sum(a.get("cost_usd") or 0 for a in run["attempts"])
        self.answer_cost = run.get("cost_usd", 0) - sql_cost if run.get("answer") else None
        self.answers_used = 0

    def complete(self, system: str, prompt: str, model: str = "sonnet",
                 max_tokens: int = 4000) -> Completion:
        if system == SQL_SYSTEM:
            if not self.sql_replies:
                raise ReplayExhausted(f"{self.run['id']}: the pipeline wants SQL attempt "
                                      f"{len(self.run['attempts']) + 1}, which was never recorded")
            a = self.sql_replies.pop(0)
            if a["error_kind"] == "provider":
                raise ProviderError(a["error"])
            text = f"```sql\n{a['sql']}\n```" if a["sql"] is not None else "(no SQL in the reply)"
            return Completion(text=text, model=a.get("model") or resolve_model(model),
                              provider=self.name, cost_usd=a.get("cost_usd"),
                              latency_ms=a.get("latency_ms") or 0.0, cached=True)
        if system == ANSWER_SYSTEM:
            if not self.run.get("answer") or self.answers_used:
                raise ReplayExhausted(f"{self.run['id']}: the pipeline wants an answer sentence, "
                                      "which was never recorded")
            self.answers_used += 1
            return Completion(text=self.run["answer"],
                              model=self.run.get("answer_model") or resolve_model(model),
                              provider=self.name, cost_usd=self.answer_cost, cached=True)
        raise ReplayExhausted(f"unknown system prompt for {self.run['id']}")


def _question(run: dict) -> dict:
    keys = ("id", "difficulty", "type", "tags", "question", "ordered", "gold_sql", "note")
    return {k: run[k] for k in keys if k in run and run[k] is not None}


def outcome(run: dict) -> tuple:
    """The parts of a run that a pipeline or scoring change could alter."""
    return (run["ok"], run["n_attempts"], run["row_count"], run["correct"],
            run["correct_strict"], run.get("inconclusive"),
            [(a["error_kind"], a["row_count"]) for a in run["attempts"]])


def rescore_set(entry: dict) -> tuple[dict, list[str]]:
    meta = entry["meta"]
    db_path = ROOT / "web" / "data" / meta["database"]
    schema = schema_summary(str(db_path), samples=meta.get("schema", "full") == "full")
    write_answer = {"all": True, "none": False, "retried": "retried"}[meta["answers_written_for"]]
    runs, changes = [], []
    for old in entry["runs"]:
        new = evaluate_one(_question(old), ReplayProvider(old), db_path, schema,
                           meta["sql_model"], meta.get("answer_model") or "haiku", write_answer)
        new["answer"] = old["answer"]          # never regenerated
        new["answer_model"] = old.get("answer_model")
        if round(new["cost_usd"], 4) != round(old["cost_usd"], 4):
            raise AssertionError(f"{old['id']}: replayed cost {new['cost_usd']} != {old['cost_usd']}")
        if outcome(new) != outcome(old):
            changes.append(f"{entry['name']}/{old['id']}: {outcome(old)} -> {outcome(new)}")
        # Preserve any source metadata the current evaluator does not emit.
        runs.append(dict(old, **new))
    out = dict(entry, runs=runs, summary=summarize(runs))
    return out, changes


def rescore(path: Path = RUNS, write: bool = True) -> list[str]:
    data = json.loads(Path(path).read_text())
    all_changes, sets = [], []
    for entry in data["sets"]:
        new, changes = rescore_set(entry)
        all_changes += changes
        old_s, new_s = entry["summary"], new["summary"]
        for k in ("overall", "overall_strict"):
            if old_s[k] != new_s[k]:
                all_changes.append(f"{entry['name']} {k}: {old_s[k]['correct']} -> {new_s[k]['correct']}"
                                   f" / {new_s[k]['n']}")
        new["meta"] = dict(new["meta"], rescored=dt.date.today().isoformat(),
                           rescore_note="recomputed from the recorded model replies with the current "
                                        "pipeline and scorer (sqlask-rescore); no new model calls")
        sets.append(new)
    if write:
        data["sets"] = sets
        Path(path).write_text(json.dumps(data, indent=1, ensure_ascii=True) + "\n")
    return all_changes


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Recompute recorded runs without model calls")
    p.add_argument("--runs", default=str(RUNS))
    p.add_argument("--check", action="store_true", help="report changes, do not write")
    args = p.parse_args(argv)
    changes = rescore(Path(args.runs), write=not args.check)
    print("\n".join(changes) if changes else "no outcome changed")


if __name__ == "__main__":
    main()
