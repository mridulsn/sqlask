# sqlask: a text-to-SQL analyst

Ask a SQLite database a question in plain English. A model writes one `SELECT`, a read-only
guard checks it, the query runs, and if it fails (SQL error, or an empty result that looks
wrong) the error text and the failed SQL go back to the model for another try, up to 3
attempts. You get a short plain-English answer, the SQL that produced it, and the rows.

Live demo (static, no server, no API key; to be published at `https://mridulsaravanan.com/sqlask/`).
It replays recorded model runs step by step, re-executes every query in the browser with
sql.js, lets you run your own SQL through the same guard, and shows the eval scoreboard.

```
 question
    |
    v
 schema summary  ---  tables, columns, types, PK/FK, row counts, sample values
    |
    v
 model writes ONE SQLite SELECT  <------------------------------+
    |                                                            |
    v                                                            |
 read-only guard (guard.py)                                      |
   - one statement, starts with SELECT or WITH                   |
   - no write/DDL/PRAGMA/ATTACH keywords outside literals        | error text +
   - outermost LIMIT added (200) or capped                       | failed SQL
    |                                                            | (max 3 attempts)
    v                                                            |
 execute on a read-only connection (db.py)                      |
   - file opened mode=ro, PRAGMA query_only=ON                   |
   - authorizer allows only SELECT/READ/FUNCTION/RECURSIVE       |
   - 5 s time limit via progress handler                         |
    |                                                            |
    +-- SQL error, guard rejection, no SQL, timeout -------------+
    +-- no rows, or one row of NULL/0 (suspicious empty) -------+
    |      (if the model returns the same SQL again, the empty
    |       result is accepted as the real answer)
    v
 model writes a 1-3 sentence answer from the rows
    |
    v
 {answer, sql, columns, rows, attempts[]}
```

## Run it

Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run pytest                                  # offline, uses FakeProvider (59 tests)

uv run sqlask "How many invoices were billed to the UK?"
uv run sqlask "Which artist has earned the most revenue?" --json

uv run sqlask-eval                             # full eval, writes web/data/runs.json
uv run sqlask-eval --sql-model haiku --answers retried --set-name haiku
uv run sqlask-eval --provider fake             # smoke test: echoes the gold SQL, no model calls
```

Which model backend is used:

| Provider | When | Notes |
|---|---|---|
| `AnthropicProvider` | `ANTHROPIC_API_KEY` is set (or `--provider anthropic`) | official `anthropic` SDK; adaptive thinking (model default), `effort: medium`; Sonnet/Opus calls opt into server-side refusal fallbacks; typed error chain mapped to `ProviderError` |
| `ClaudeCLIProvider` | no key (default locally) | runs `claude -p` headless with MCP servers, hooks, built-in tools and session persistence all switched off, from an empty temp dir; replies cached on disk in `.cache/` |
| `FakeProvider` | tests | scripted replies |

Example output (recorded with the CLI provider):

```
$ uv run sqlask "How many invoices were billed to the UK?" --provider cli
[attempt 1] ok, 1 rows

SQL:
SELECT COUNT(*) AS InvoiceCount
FROM Invoice
WHERE BillingCountry = 'United Kingdom';

InvoiceCount
21

Answer: Twenty-one invoices were billed to the United Kingdom.
```

Web demo locally: `cd web && python3 -m http.server 8000`, then open `http://localhost:8000/`.

## Data

`web/data/chinook.sqlite` is the [Chinook sample database](https://github.com/lerocha/chinook-database)
(release v1.4.5, `Chinook_Sqlite.sqlite`, re-packed with `VACUUM INTO` to drop free pages; no rows
changed). Copyright (c) 2008-2024 Luis Rocha, MIT licence; the full notice is in
`web/data/CHINOOK-LICENSE.md`. It models a music store: 11 tables (Artist, Album, Track, Genre,
MediaType, Playlist, PlaylistTrack, Customer, Employee, Invoice, InvoiceLine), invoices 2009-2013.
The customer and employee records are sample data.

## Evaluation

`eval/questions.json` has 40 questions with hand-written gold SQL: 12 easy (filters, counts),
14 medium (joins, group-by, top-N, dates, self-join), 14 hard (anti-joins, nested aggregates,
window functions, per-group maximum with ties, a question whose true answer is empty). Every gold
query is checked by the tests to pass the guard and run.

**Scoring is execution accuracy:** the predicted result must equal the gold result. Rows are
compared as a multiset (as a sequence only when the question asks for an order), numbers are
rounded to 2 decimals and numeric strings like `'2011'` count as numbers. Column names and order
do not matter, and extra columns are allowed (asking "which artist" and getting
`Name, Revenue` is fine). Two adjacent text columns in the gold result (FirstName, LastName) also
match one `'First Last'` column. "Strict" turns off the extra-column and name-join tolerance.

### Results (recorded 2026-11-19, Claude Code CLI 2.1.328, `claude -p`)

SQL generation used **Claude Sonnet 5.5** (`claude-sonnet-5-5`) for the main run, as the
headline configuration. The answer sentences used **Claude Haiku 5.5** (`claude-haiku-5-5`),
because turning a few rows into a sentence does not need a larger model. A second run used Haiku
5.5 for the SQL too, to see whether the cheaper model holds up.

| SQL model | Schema in prompt | Correct | Strict | Needed a retry | Retry then correct | Median SQL call | Cost (CLI-reported) |
|---|---|---|---|---|---|---|---|
| Sonnet 5.5 | full | **40 / 40** | 27 / 40 | 1 | 1 | 3.8 s | $1.03 incl. 40 answers |
| Haiku 5.5 | full | **40 / 40** | 26 / 40 | 1 | 1 | 4.4 s | $0.057 (answers only for retried) |
| Haiku 5.5 | bare, 6 value-sensitive questions | 6 / 6 | 6 / 6 | 0 | 0 | 4.4 s | $0.005 |

By difficulty (Sonnet 5.5): easy 12/12, medium 14/14, hard 14/14. Haiku 5.5 is the same.

Retries: the one question that needed a second attempt in both runs is h02 ("customers who
have never bought a Rock track"). The true answer is empty, so attempt 1 was flagged as a
suspicious empty result and sent back; the model returned the same query, which the pipeline
treats as confirmation, and the result was scored correct. No SQL errors, guard rejections or
timeouts happened in the scored runs.

Cost: the CLI reports cost per call. A Sonnet SQL call averaged $0.025 and a Haiku SQL call
$0.0014; most of that is the ~2,000-token schema summary sent with every call.

The **bare** row is an ablation: the same pipeline with the sample values and the date-format
note removed from the schema, on the six questions most likely to need an exact stored value
("billed to the UK", "Sales Support Agents", "cities in the United States", "hip hop tracks",
"invoices in 2011", "percentage from Rock"). Haiku still got all six on the first try by writing
defensive filters: `BillingCountry IN ('United Kingdom', 'UK')`, `g.Name LIKE '%hip%hop%'`,
`LOWER(Title) = 'sales support agent'`.

### Demo prompts (recorded, not scored)

`eval/demo_questions.json` holds four prompts meant to exercise the failure paths. They have no
gold query; they are replayed on the demo page.

| Prompt | What happened |
|---|---|
| "How many tracks are in the 'Hip Hop' genre?" | Attempt 1 filtered `Genre.Name = 'Hip Hop'` and got `COUNT = 0`; flagged as suspicious and sent back; attempt 2 used `'Hip Hop/Rap'` and returned 35. |
| "How many invoices were billed to Bengaluru?" | First try used `BillingCity = 'Bangalore'` (the stored spelling), 6 invoices. |
| "Delete every track that is longer than an hour." | The model wrote a `SELECT` of the two tracks a delete would remove, with a comment that the session is read-only. Had it written a `DELETE`, the guard and the authorizer would have blocked it (both are tested). |
| "What is the average age of our customers?" | The Customer table has no birth date. The model returned `SELECT NULL` with a comment explaining why; flagged as suspicious, returned unchanged, accepted; the answer says the data cannot answer it. |

### Failure cases I saw

- **The benchmark is saturated.** 40/40 for both models means this question set does not
  separate them and barely exercises the retry loop. Chinook is one of the most widely used
  sample databases, and the bare-schema ablation suggests the models either know its values or
  hedge well enough that it does not matter. A harder test needs a database the models have not
  seen (or Chinook with renamed tables and perturbed values) and questions written by someone
  who has not read the schema.
- **The answer step makes mistakes that the score does not catch.** Execution accuracy only
  scores the SQL result. The recorded answers include: h11 says "Across 25 countries" (there are
  24) and lists Luis Rojas ($46.62) before Richard Cunningham ($47.62) as "followed by"; m04 says
  Greatest Hits "ties for the top spot with no other album sharing its track count".
- **The answer prompt causes filler.** The rule "if the rows were cut off, say the list is
  partial" made Haiku add sentences like "The result contains only this one row, so it is the
  full answer and not a partial list" to many answers. The fix is to mention truncation only
  when it happened (the prompt already gets that flag) and drop the general rule.
- **Strict accuracy is much lower (27/40 and 26/40)** because the models add useful columns:
  counts next to names, revenue next to the top artist, ids next to names. That is why the main
  metric allows extra columns.
- **Gold queries can be ambiguous.** In a first draft, "Which playlists contain more than 1000
  tracks?" had a gold result with two rows named "Music" (two playlists share the name); a query
  that grouped by name would return one. The question now asks for id and name. Similar care
  went into ties (h11 and h12 ask for ties explicitly and the gold keeps them).

## Design decisions and trade-offs

- **One SELECT, not a conversation with tools.** The model gets the schema and the question and
  must return one statement. That keeps the attack surface small and makes each attempt easy to
  record and replay. The cost is that it cannot explore the data (for example, look up the
  distinct values of a column) before writing the query.
- **Three layers of read-only protection.** The text guard is not a SQL parser: it masks string
  literals, quoted identifiers and comments, then checks keywords and statement count. Because a
  regex-level check can be fooled, the connection is also opened read-only with `query_only`, and
  an SQLite authorizer denies every action except reading. A test runs `DELETE`, `DROP`,
  `INSERT`, `PRAGMA` straight against the connection (no guard) and checks they fail.
- **LIMIT is enforced on the outermost query only.** A top-level `LIMIT` is added or lowered to
  200; anything unusual (`LIMIT 2 + 2`) is wrapped as `SELECT * FROM (...) LIMIT 200`.
- **"Suspicious empty" is a heuristic:** zero rows, or one row where every value is NULL or 0.
  It catches the common failure (a filter value that does not exist) but costs an extra call
  when the true answer is empty or zero. The model can confirm by returning the same query.
- **Schema summary with sample values.** Categorical text columns with up to 12 distinct values
  list all of them; other columns show the 3 most frequent values. On a larger database this is
  where token cost grows.
- **Same guard in Python and JavaScript.** `web/app.js` carries a port of `guard.py`; a pytest
  runs the JS port on the same 28 cases in `tests/guard_cases.json` (skipped if `node` is not
  installed). The scorer is ported too, so the demo re-checks each replayed run against its gold
  query live.
- **Recorded, not invented.** Every model output on the demo page comes from
  `web/data/runs.json`, written by `sqlask-eval` from real CLI calls (139 calls in total, cached
  in `.cache/`, so rerunning the eval costs nothing).

## Limitations

- SQLite only; the prompt, guard and date handling assume SQLite syntax.
- The guard is a keyword check, not a parser; the read-only connection is the real boundary.
- The in-browser database runs in a Web Worker and a query is stopped after 8 seconds; the
  Python side stops queries after 5 seconds.
- The answer text is not scored, and it can contain errors (see above).
- The recorded runs went through the Claude Code CLI, not the API directly; costs are what the
  CLI reported, and latency includes CLI start-up.
- 40 questions on one well-known database is a small, easy benchmark.

## Layout

```
src/sqlask/guard.py      read-only guard (masking, keyword checks, LIMIT)
src/sqlask/db.py         read-only connection, authorizer, time limit
src/sqlask/schema.py     schema summary for the prompt (full or bare)
src/sqlask/providers.py  AnthropicProvider, ClaudeCLIProvider, FakeProvider
src/sqlask/pipeline.py   ask(): attempts, retry feedback, answer step
src/sqlask/scoring.py    execution-accuracy comparison
src/sqlask/evaluate.py   eval runner (sqlask-eval), writes web/data/runs.json
eval/questions.json      40 scored questions with gold SQL
eval/demo_questions.json 4 unscored demo prompts
tests/                   pytest suite (offline) + guard_cases.json shared with the JS port
web/                     static demo: index.html, app.js, style.css, data/
```
