import json
import shutil
import subprocess
from pathlib import Path

import pytest

from sqlask.guard import GuardError, check_sql, mask_sql

CASES = json.loads((Path(__file__).parent / "guard_cases.json").read_text())
ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("case", CASES["cases"], ids=lambda c: c["sql"][:40] or "empty")
def test_guard_cases(case):
    if case["ok"]:
        g = check_sql(case["sql"], max_rows=CASES["max_rows"])
        if "expect_sql" in case:
            assert g.sql == case["expect_sql"]
    else:
        with pytest.raises(GuardError) as e:
            check_sql(case["sql"], max_rows=CASES["max_rows"])
        assert case["reason"].lower() in str(e.value).lower()


def test_mask_keeps_length_and_hides_literals():
    sql = "SELECT 'DROP;' AS \"x;y\" -- DELETE\nFROM t"
    m = mask_sql(sql)
    assert len(m) == len(sql)
    assert "DROP" not in m and "DELETE" not in m and ";" not in m


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_js_guard_matches_python():
    """web/app.js carries a port of the guard; run it on the same cases."""
    script = (
        "const g = require(process.argv[1]);"
        "const cases = require(process.argv[2]);"
        "const out = cases.cases.map(c => { try { return {ok: true, sql: g.checkSql(c.sql, cases.max_rows).sql}; }"
        " catch (e) { return {ok: false, reason: String(e.message)}; } });"
        "console.log(JSON.stringify(out));"
    )
    res = subprocess.run(["node", "-e", script, str(ROOT / "web" / "app.js"),
                          str(Path(__file__).parent / "guard_cases.json")],
                         capture_output=True, text=True, check=True)
    js = json.loads(res.stdout)
    for case, got in zip(CASES["cases"], js):
        if case["ok"]:
            assert got["ok"], (case["sql"], got)
            assert got["sql"] == check_sql(case["sql"], CASES["max_rows"]).sql
        else:
            assert not got["ok"], case["sql"]
            assert case["reason"].lower() in got["reason"].lower()
