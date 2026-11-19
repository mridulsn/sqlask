"""Offline regressions for findings 1-11 in qc-sqlask.md."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from sqlask.db import DEFAULT_DB, open_readonly
from sqlask.evaluate import evaluate_one
from sqlask.pipeline import ask
from sqlask.providers import FakeProvider, ProviderError
from sqlask.scoring import norm, results_match

ROOT = Path(__file__).resolve().parents[1]
CASES = json.loads((Path(__file__).parent / "scoring_cases.json").read_text())


def fence(sql):
    return f"```sql\n{sql}\n```"


@pytest.fixture
def conn():
    c = open_readonly()
    yield c
    c.close()


@pytest.mark.parametrize("case", CASES["empty"])
def test_f01_empty_shape_python(case):
    assert results_match([], [], strict=case["strict"], gold_ncols=case["gold"],
                         pred_ncols=case["pred"]) == case["match"]


@pytest.mark.parametrize("replies", [
    ["SELECT missing FROM Track"] * 2,
    ["DELETE FROM Track"] * 2,
    [ProviderError("offline")],
])
def test_f02_failed_retries_do_not_accept_provisional_result(conn, replies):
    fake = FakeProvider([fence("SELECT 0")] +
                        [fence(r) if isinstance(r, str) else r for r in replies])
    result = ask("?", fake, conn, "", write_answer=False)
    assert not result.ok
    assert result.rows == []
    assert "confirmed" in result.answer


def evaluate(sql, gold="SELECT TrackId FROM Track ORDER BY TrackId"):
    q = dict(id="regression", difficulty="easy", question="List every track ID",
             gold_sql=gold, ordered=True)
    return evaluate_one(q, FakeProvider([fence(sql)]), DEFAULT_DB, "", "fake", "fake", False)


def test_f03_matching_prefix_is_not_a_complete_answer():
    result = evaluate("SELECT TrackId FROM Track WHERE TrackId <= 200 ORDER BY TrackId")
    assert result["correct"] is False and result["correct_strict"] is False
    assert result["gold_row_count"] == 3503


def test_f03_complete_large_answer_is_correct():
    result = evaluate("SELECT TrackId FROM Track ORDER BY TrackId")
    assert result["correct"] is True and result["correct_strict"] is True
    assert result["gold_row_count"] == 3503


def test_f03_evaluation_cap_is_inconclusive(monkeypatch):
    import sqlask.evaluate as ev
    monkeypatch.setattr(ev, "EVAL_MAX_ROWS", 200, raising=False)
    result = evaluate("SELECT TrackId FROM Track ORDER BY TrackId",
                      "SELECT TrackId FROM Track ORDER BY TrackId LIMIT 200")
    assert result["correct"] is None and result["correct_strict"] is None
    assert result["inconclusive"]
    assert ev.summarize([result])["overall"] == dict(n=1, correct=0, accuracy=0.0)


@pytest.mark.parametrize("case", CASES["numbers"])
def test_f04_python_rounding(case):
    assert norm(case["value"]) == case["expected"]


@pytest.mark.parametrize("first,second", [
    ("Atlantis", "ATLANTIS"), ("a  b", "a b"), ("a''B", "a''b"),
])
def test_f07_changed_literal_is_not_confirmation(conn, first, second):
    fake = FakeProvider([fence(f"SELECT 1 WHERE 'x' = '{v}'") for v in (first, second)] +
                        [fence("SELECT 42")])
    result = ask("?", fake, conn, "", write_answer=False)
    assert len(result.attempts) == 3 and result.rows == [[42]]


@pytest.mark.parametrize("sql", ["SELECT Name FROM Track", "SELECT Name FROM Track LIMIT 999",
                                 "SELECT Name FROM Track LIMIT 1 + 999"])
def test_f08_report_and_answer_prompt_use_executed_sql(conn, sql):
    executed = []
    conn.set_trace_callback(executed.append)
    fake = FakeProvider([fence(sql), "A partial list."])
    result = ask("List tracks", fake, conn, "")
    assert result.sql == executed[-1] == result.attempts[-1].executed_sql
    assert result.model_sql == sql
    assert result.sql in fake.calls[-1]["prompt"]


@pytest.mark.parametrize("operation", ["db", "schema", "disguise", "cli"])
def test_f09_relative_paths_with_uri_characters(tmp_path, monkeypatch, capsys, operation):
    from sqlask import main
    from sqlask.disguise import build
    from sqlask.schema import schema_summary
    import sqlask
    name = "music ?# library.sqlite"
    shutil.copyfile(DEFAULT_DB, tmp_path / name)
    monkeypatch.chdir(tmp_path)
    if operation == "db":
        c = open_readonly(name)
        try:
            assert c.execute("SELECT COUNT(*) FROM Track").fetchone()[0] == 3503
        finally:
            c.close()
    elif operation == "schema":
        assert "TABLE Track" in schema_summary(name)
    elif operation == "disguise":
        assert build(name, "output file.sqlite").exists()
    else:
        monkeypatch.setattr(sqlask, "default_provider", lambda: FakeProvider([
            fence("SELECT COUNT(*) FROM Track"), "3503 tracks."]))
        main(["Count tracks", "--db", name, "--json"])
        assert json.loads(capsys.readouterr().out)["rows"] == [[3503]]


@pytest.mark.parametrize("finding", ["f01", "f03", "f03_cap", "f04", "f05", "f05_error", "f05_button", "f06", "f10", "f11"])
def test_browser_regressions(finding):
    result = subprocess.run(["node", str(Path(__file__).with_name("browser_regressions.js")), finding],
                            cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
