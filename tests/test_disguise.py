import json
import sqlite3
from pathlib import Path

import pytest

from sqlask.db import DEFAULT_DB, open_readonly, run_query
from sqlask.disguise import DISGUISED_DB, TABLE_NAMES, build
from sqlask.guard import check_sql

ROOT = Path(__file__).resolve().parents[1]
HARD = json.loads((ROOT / "eval" / "hard_questions.json").read_text())


@pytest.fixture(scope="module")
def fresh(tmp_path_factory):
    return build(DEFAULT_DB, tmp_path_factory.mktemp("d") / "medialib.sqlite")


def dump(path):
    conn = sqlite3.connect(path)
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    out = {t: conn.execute(f"SELECT * FROM {t} ORDER BY 1, 2").fetchall() for t in tables}
    conn.close()
    return out


def test_build_is_deterministic_and_committed_copy_is_current(fresh):
    assert dump(fresh) == dump(DISGUISED_DB)


def test_no_original_names_left(fresh):
    conn = sqlite3.connect(fresh)
    ddl = " ".join(r[0] for r in conn.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"))
    for old in list(TABLE_NAMES) + ["Milliseconds", "UnitPrice", "InvoiceDate", "SupportRepId"]:
        assert old.lower() not in ddl.lower(), old
    assert conn.execute("SELECT COUNT(*) FROM style WHERE style_label = 'Hip Hop/Rap'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM member WHERE nation IN ('USA', 'United Kingdom')").fetchone()[0] == 0


def test_rows_preserved_and_values_transformed(fresh):
    src, dst = sqlite3.connect(DEFAULT_DB), sqlite3.connect(fresh)
    for old, new in TABLE_NAMES.items():
        assert src.execute(f"SELECT COUNT(*) FROM {old}").fetchone() == \
            dst.execute(f"SELECT COUNT(*) FROM {new}").fetchone(), old
    # dates shifted by 2963 days 9:30, prices x75, totals still add up
    assert dst.execute("SELECT checked_out_at FROM checkout WHERE checkout_id = 1").fetchone()[0] == \
        "2017-02-11 09:30:00"
    assert {r[0] for r in dst.execute("SELECT DISTINCT price FROM catalog_item")} == {74.25, 149.25}
    bad = dst.execute("""SELECT COUNT(*) FROM checkout k WHERE ABS(k.amount -
        (SELECT SUM(price_each * qty) FROM checkout_line l WHERE l.checkout_id = k.checkout_id)) > 0.001""")
    assert bad.fetchone()[0] == 0


def test_hard_set_is_well_formed():
    assert len(HARD) == 20 and len({q["id"] for q in HARD}) == 20
    assert {"value", "hidden-value", "multi-join", "window", "date-range", "ties", "null"} == \
        {q["type"] for q in HARD}
    conn = open_readonly(DISGUISED_DB)
    for q in HARD:
        res = run_query(conn, check_sql(q["gold_sql"]).sql)
        assert res.rows, q["id"]  # every gold answer is non-empty


def test_fake_eval_on_disguised_suite(tmp_path):
    from sqlask.evaluate import main
    out = tmp_path / "runs.json"
    main(["--provider", "fake", "--suite", "disguised", "--out", str(out), "--parallel", "1",
          "--set-name", "d", "--answers", "none"])
    s = json.loads(out.read_text())["sets"][0]
    assert s["meta"]["database"] == "medialib.sqlite" and s["meta"]["suite"] == "disguised"
    assert s["summary"]["overall"]["accuracy"] == 1.0
    assert s["summary"]["by_type"]["null"] == {"n": 3, "correct": 3, "accuracy": 1.0}
