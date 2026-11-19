import json
from pathlib import Path

import pytest

from sqlask.db import open_readonly
from sqlask.pipeline import ask, extract_sql
from sqlask.providers import ClaudeCLIProvider, FakeProvider, ProviderError
from sqlask.schema import schema_summary


@pytest.fixture(scope="module")
def schema():
    return schema_summary()


@pytest.fixture()
def conn():
    c = open_readonly()
    yield c
    c.close()


def fence(sql):
    return f"```sql\n{sql}\n```"


def test_extract_sql():
    assert extract_sql("Here:\n```sql\nSELECT 1;\n```\nthanks") == "SELECT 1;"
    assert extract_sql("SELECT 2") == "SELECT 2"
    assert extract_sql("Sure! WITH a AS (SELECT 1) SELECT * FROM a") == "WITH a AS (SELECT 1) SELECT * FROM a"
    assert extract_sql("I cannot answer that.") is None


def test_happy_path(conn, schema):
    fake = FakeProvider([fence("SELECT COUNT(*) AS n FROM Customer"), "There are 59 customers."])
    res = ask("How many customers?", fake, conn, schema)
    assert res.ok and res.rows == [[59]] and res.columns == ["n"]
    assert res.answer == "There are 59 customers."
    assert len(res.attempts) == 1 and res.attempts[0].error is None
    assert fake.calls[0]["model"] == "sonnet" and fake.calls[1]["model"] == "haiku"
    assert "TABLE Customer" in fake.calls[0]["prompt"]
    assert "Rows (1)" in fake.calls[1]["prompt"]


def test_sql_error_is_fed_back_and_fixed(conn, schema):
    fake = FakeProvider([
        fence("SELECT COUNT(*) FROM Customers"),          # wrong table name
        fence("SELECT COUNT(*) FROM Customer"),
        "59.",
    ])
    res = ask("How many customers?", fake, conn, schema)
    assert res.ok and res.rows == [[59]]
    assert [a.error_kind for a in res.attempts] == ["sql_error", None]
    retry_prompt = fake.calls[1]["prompt"]
    assert "no such table: Customers" in retry_prompt
    assert "SELECT COUNT(*) FROM Customers" in retry_prompt


def test_suspicious_empty_triggers_retry(conn, schema):
    fake = FakeProvider([
        fence("SELECT COUNT(*) FROM Invoice WHERE BillingCountry = 'UK'"),   # COUNT = 0
        fence("SELECT COUNT(*) FROM Invoice WHERE BillingCountry = 'United Kingdom'"),
        "21 invoices.",
    ])
    res = ask("How many invoices were billed to the UK?", fake, conn, schema)
    assert res.rows == [[21]]
    assert res.attempts[0].error_kind == "empty"
    assert "return the same query unchanged" in fake.calls[1]["prompt"]


def test_confirmed_empty_is_accepted(conn, schema):
    sql = "SELECT FirstName FROM Customer WHERE Country = 'Atlantis'"
    fake = FakeProvider([fence(sql), fence(sql + ";"), "No matching customers."])
    res = ask("Customers in Atlantis?", fake, conn, schema)
    assert res.ok and res.rows == [] and len(res.attempts) == 2
    assert res.attempts[1].error is None


def test_guard_rejection_and_parse_failure_count_as_attempts(conn, schema):
    fake = FakeProvider([
        fence("DELETE FROM Track"),
        "I am not sure.",
        fence("SELECT COUNT(*) FROM Track"),
        "3503 tracks.",
    ])
    res = ask("How many tracks?", fake, conn, schema)
    assert [a.error_kind for a in res.attempts] == ["guard", "parse", None]
    assert res.rows == [[3503]]


def test_gives_up_after_three_attempts(conn, schema):
    fake = FakeProvider([fence("SELECT nope FROM Track")] * 3)
    res = ask("?", fake, conn, schema)
    assert not res.ok and len(res.attempts) == 3
    assert "Could not produce a working query" in res.answer
    assert len(fake.calls) == 3   # no answer call when nothing ran


def test_provider_error_stops(conn, schema):
    fake = FakeProvider([ProviderError("network down")])
    res = ask("?", fake, conn, schema)
    assert not res.ok and res.attempts[0].error_kind == "provider"


def test_limit_enforced(conn, schema):
    fake = FakeProvider([fence("SELECT Name FROM Track"), "Many tracks."])
    res = ask("List all tracks", fake, conn, schema)
    assert len(res.rows) == 200 and res.truncated
    assert "Only the first 50 rows" in fake.calls[1]["prompt"]


def test_cli_provider_uses_isolation_flags(tmp_path):
    cmd = ClaudeCLIProvider(cache_dir=tmp_path).command("SYS", "sonnet")
    for flag in ["-p", "--strict-mcp-config", "--no-session-persistence", "--tools"]:
        assert flag in cmd
    assert cmd[cmd.index("--mcp-config") + 1] == '{"mcpServers":{}}'
    assert json.loads(cmd[cmd.index("--settings") + 1]) == {"disableAllHooks": True}
    assert cmd[cmd.index("--tools") + 1] == ""


def test_cli_provider_reads_cache(tmp_path, monkeypatch):
    prov = ClaudeCLIProvider(cache_dir=tmp_path, binary="definitely-not-installed")
    key = prov._key("S", "P", "haiku")
    (tmp_path / f"{key}.json").write_text(json.dumps({
        "text": "cached!", "model": "claude-haiku-5-5", "provider": "claude-cli"}))
    c = prov.complete("S", "P", model="haiku")
    assert c.text == "cached!" and c.cached
    with pytest.raises(ProviderError):
        prov.complete("S", "other prompt", model="haiku")


def test_gold_set_is_well_formed():
    qs = json.loads((Path(__file__).resolve().parents[1] / "eval" / "questions.json").read_text())
    assert len(qs) >= 30
    assert {q["difficulty"] for q in qs} == {"easy", "medium", "hard"}
    assert len({q["id"] for q in qs}) == len(qs)


def test_fake_eval_end_to_end(tmp_path):
    from sqlask.evaluate import main
    out = tmp_path / "runs.json"
    main(["--provider", "fake", "--out", str(out), "--parallel", "1"])
    data = json.loads(out.read_text())
    assert data["summary"]["overall"]["accuracy"] == 1.0
    assert all(r["final_sql"] for r in data["runs"])
