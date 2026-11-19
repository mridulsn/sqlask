"""Keep the published results reproducible without model calls."""
import json
import statistics
import subprocess
from pathlib import Path

import pytest

from sqlask.db import ROOT, open_readonly
from sqlask.evaluate import full_result, score
from sqlask.replay import ReplayExhausted, ReplayProvider, rescore_set
from sqlask.pipeline import SQL_SYSTEM

DATA = json.loads((ROOT / "web/data/runs.json").read_text())
SETS = {entry["name"]: entry for entry in DATA["sets"]}


@pytest.mark.parametrize("entry", DATA["sets"], ids=lambda entry: entry["name"])
def test_recorded_summaries_recompute_exactly(entry, monkeypatch):
    from sqlask.providers import AnthropicProvider, ClaudeCLIProvider

    def forbidden(*args, **kwargs):
        pytest.fail("offline replay must never call a model")

    monkeypatch.setattr(AnthropicProvider, "complete", forbidden)
    monkeypatch.setattr(ClaudeCLIProvider, "complete", forbidden)
    recomputed, changes = rescore_set(entry)
    assert not changes
    assert recomputed["summary"] == entry["summary"]
    for old, new in zip(entry["runs"], recomputed["runs"]):
        for key in ("answer", "answer_model", "cost_usd", "model_sql", "final_sql", "attempts"):
            assert new[key] == old[key], (old["id"], key)


def test_missing_recording_never_falls_back_to_a_model():
    provider = ReplayProvider(dict(id="missing", attempts=[], answer="", cost_usd=0))
    with pytest.raises(ReplayExhausted, match="never recorded"):
        provider.complete(SQL_SYSTEM, "need another attempt")


def test_all_recorded_scores_match_javascript():
    comparisons = []
    for entry in DATA["sets"]:
        conn = open_readonly(ROOT / "web/data" / entry["meta"]["database"])
        try:
            for run in entry["runs"]:
                if not run.get("gold_sql"):
                    continue
                gold = full_result(conn, run["gold_sql"])
                pred = full_result(conn, run["model_sql"])
                assert gold is not None and pred is not None
                actual = score(gold, pred, run["ordered"])
                assert actual == (run["correct"], run["correct_strict"])
                comparisons.append(dict(g=gold.rows, p=pred.rows, gc=len(gold.columns),
                                        pc=len(pred.columns), ordered=run["ordered"], expected=actual))
        finally:
            conn.close()
    script = """
const app = require('./web/app.js');
const cases = JSON.parse(require('fs').readFileSync(0, 'utf8'));
console.log(JSON.stringify(cases.map(c => [false, true].map(strict =>
  app.resultsMatch(c.g, c.p, c.ordered, strict, c.gc, c.pc)))));
"""
    result = subprocess.run(["node", "-e", script], input=json.dumps(comparisons),
                            cwd=ROOT, text=True, capture_output=True, check=True)
    assert json.loads(result.stdout) == [list(c["expected"]) for c in comparisons]


def test_readme_tables_and_call_totals_match_recordings():
    readme = (ROOT / "README.md").read_text()
    html = (ROOT / "web/index.html").read_text()
    total_calls = 0
    hard_calls = 0
    for name, entry in SETS.items():
        summary = entry["summary"]
        calls = summary["sql_calls"] + sum(bool(r["answer_model"]) for r in entry["runs"])
        total_calls += calls
        if name == "demos":
            continue
        model = "Sonnet 5.5" if "sonnet" in name else "Haiku 5.5"
        median = statistics.median(a["latency_ms"] for r in entry["runs"] for a in r["attempts"]) / 1000
        cost = summary["total_cost_usd"]
        money = f"${cost:.3f}" if cost < 0.1 else f"${cost:.2f}"
        overall, strict = summary["overall"], summary["overall_strict"]
        correct = f"{overall['correct']} / {overall['n']}"
        if name != "ablation-bare":
            correct = f"**{correct}**"
        if name.startswith("disguised"):
            hard_calls += calls
            expected = (f"| {model} | {correct} | {strict['correct']} / {strict['n']} | "
                        f"{summary['needed_retry']} ({', '.join(summary['retry_ids'])}) | "
                        f"{summary['retry_then_correct']} | {summary['wrong_without_retry']} | "
                        f"{summary['sql_calls']} | {median:.1f} s | {money} |")
        else:
            schema = f"bare, {overall['n']} value-sensitive questions" if name == "ablation-bare" else "full"
            expected = (f"| {model} | {schema} | {correct} | {strict['correct']} / {strict['n']} | "
                        f"{summary['needed_retry']} | {summary['retry_then_correct']} | {median:.1f} s | {money}")
        assert expected in readme
    assert f"{total_calls} calls in total" in readme
    assert f"{hard_calls} CLI calls in total" in readme
    assert f"Execution accuracy on {SETS['sonnet']['summary']['overall']['n']} questions" in html
    assert f"{SETS['disguised-sonnet']['summary']['overall']['n']} harder questions" in html
    for name, digits in (("sonnet", 3), ("haiku", 4)):
        costs = [a["cost_usd"] for r in SETS[name]["runs"] for a in r["attempts"]]
        assert f"${statistics.mean(costs):.{digits}f}" in readme


def test_source_is_ascii():
    paths = list((ROOT / "src").rglob("*.py")) + list((ROOT / "tests").glob("*"))
    paths += [ROOT / "web/app.js", ROOT / "web/index.html", ROOT / "README.md"]
    for path in paths:
        if path.is_file():
            assert path.read_bytes().isascii(), path
