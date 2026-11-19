# sqlask: a text-to-SQL analyst

Ask a SQLite database a question in plain English. A model writes one `SELECT`, a read-only
guard checks it, the query runs, and if it fails (SQL error, or an empty result that looks
wrong) the error text and the failed SQL go back to the model for another try, up to 3
attempts. You get a short plain-English answer, the SQL that produced it, and the rows.

Live demo (static, no server, no API key; to be published at `https://mridulsaravanan.com/sqlask/`).
It replays recorded model runs step by step, re-executes every query in the browser with
sql.js (on Chinook or the disguised copy), lets you run your own SQL through the same guard, and
shows both eval scoreboards. `index.html#run=disguised-haiku/x06` opens a specific run.

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
uv run pytest                                  # offline, uses FakeProvider (64 tests)

uv run sqlask "How many invoices were billed to the UK?"
uv run sqlask "Which artist has earned the most revenue?" --json

uv run sqlask-eval                             # full eval, writes web/data/runs.json
uv run sqlask-eval --sql-model haiku --answers retried --set-name haiku
uv run sqlask-eval --provider fake             # smoke test: echoes the gold SQL, no model calls

uv run sqlask-disguise                         # rebuild web/data/medialib.sqlite from Chinook
uv run sqlask-eval --suite disguised --sql-model sonnet --answers retried --set-name disguised-sonnet
uv run sqlask-eval --suite disguised --sql-model haiku --answers retried --set-name disguised-haiku
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

`web/data/medialib.sqlite` is a disguised copy of the same rows, built deterministically by
`src/sqlask/disguise.py` (`uv run sqlask-disguise`) for the harder eval below:

- every table and column renamed to a "media library" vocabulary: `Artist` -> `creator`,
  `Album` -> `collection`, `Track` -> `catalog_item`, `Genre` -> `style`, `MediaType` -> `file_format`,
  `Playlist` -> `shelf`, `Customer` -> `member`, `Employee` -> `staff`, `Invoice` -> `checkout`,
  `InvoiceLine` -> `checkout_line` (columns likewise: `Milliseconds` -> `duration_sec`, `SupportRepId` -> `advisor_id`)
- every date shifted by 2,963 days 9 h 30 min, so invoices run 2017-02-11 to 2022-02-01, year
  totals no longer match the well-known ones, and every timestamp has a 09:30 time of day
- prices multiplied by 75 (0.99 -> 74.25); checkout totals recomputed from their lines
- track length stored in whole seconds, file size in KB
- some stored values replaced: genres (`'Hip Hop/Rap'` -> `'Urban / Rap'`, `'Heavy Metal'` ->
  `'Metal (Classic)'`, `'Classical'` -> `'Orchestral'`, ...), media types (`'MPEG audio file'` -> `'mp3'`),
  job titles (`'Sales Support Agent'` -> `'Customer Advisor'`, `'IT Staff'` -> `'Systems Technician'`), and
  six countries (`'USA'` -> `'US'`, `'United Kingdom'` -> `'GB'`, `'Brazil'` -> `'Brasil'`,
  `'Germany'` -> `'Deutschland'`, `'Czech Republic'` -> `'Czechia'`, `'Netherlands'` -> `'NL'`)

A test rebuilds it and checks the committed file has the same rows, that no original table or
column name is left, and that every checkout total still equals the sum of its lines.

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

**This first set was too easy.** Both models scored 40/40, the retry loop fired once per model,
and the bare-schema ablation suggests the models know Chinook well enough that the sample values
barely matter. That is why the harder set below exists.

### Harder set: disguised Chinook (recorded 2026-11-19, same CLI)

`eval/hard_questions.json` has 20 questions with hand-written gold SQL on `medialib.sqlite`, written
to hit the places where text-to-SQL usually goes wrong. Each has a type:

| Type | n | What it tests |
|---|---|---|
| value | 3 | the stored value differs from the natural wording but is shown in the schema summary (`'US'`, `'Brasil'`, "IT team" = `'Systems Lead'`/`'Systems Technician'`) |
| hidden-value | 3 | the stored value is not in the summary (only the 3 most common values of a 25-value column are shown): `'Urban / Rap'`, `'GB'`, `'Metal (Classic)'` |
| multi-join | 3 | joins across 3-5 tables (revenue per advisor, top creators outside US/Canada) |
| window | 3 | per-group maximum with a three-way tie, running total, Nth longest |
| date-range | 3 | first half / third quarter of 2019 where rows sit on 30 June and 30 September at 09:30, so `BETWEEN '2019-01-01' AND '2019-06-30'` drops them; first checkout per member |
| ties | 2 | "the top-spending member in 2019" (two tie), "the shelf with the most items" (two shelves, both named 'Music') |
| null | 3 | "not in California" where 29 members have no province (`province <> 'CA'` gives 27, not 56); `NOT IN` over a column with a NULL; LEFT JOIN that must keep a zero row |

Same pipeline, same schema summary, same scoring, retry loop on (max 3 attempts). The answer
sentence was written only for questions that needed a retry, to stay inside the call budget.

| SQL model | Correct | Strict | Needed a retry | Retry then correct | Wrong with no retry | SQL calls | Median SQL call | Cost (CLI-reported) |
|---|---|---|---|---|---|---|---|---|
| Sonnet 5.5 | **19 / 20** | 15 / 20 | 2 (x05, x06) | 1 | 0 | 23 | 3.3 s | $0.56 |
| Haiku 5.5 | **19 / 20** | 16 / 20 | 2 (x05, x06) | 1 | 0 | 22 | 3.7 s | $0.031 |

By type, both models: value 3/3, hidden-value 2/3, multi-join 3/3, window 3/3, date-range 3/3,
ties 2/2, null 3/3. 49 CLI calls in total for the two runs (45 SQL, 4 answers).

What happened:

- **The disguise did not separate the models either.** Both scored 19/20 and missed the same
  question. Hiding the names and values removed memorisation, but the date, tie, NULL and join
  traps caught neither model: both used half-open ranges (`>= '2019-01-01' AND < '2019-07-01'`),
  kept tied rows by comparing with a `MAX(...)` subquery instead of `LIMIT 1`, handled the missing
  provinces (`IS NOT 'CA'`, `COALESCE`), used `NOT EXISTS` instead of `NOT IN`, and kept the zero row
  with a LEFT JOIN.
- **The retry loop now does real work.** x05 ("revenue billed to the United Kingdom"): Sonnet tried
  `'United Kingdom'`, then `'UK'`, both returned `SUM = NULL` and were sent back, and attempt 3 used
  `UPPER(TRIM(bill_nation)) IN ('UNITED KINGDOM', 'UK', ..., 'GB', ...)` and was correct. Haiku went
  from `IN ('United Kingdom', 'UK')` to a wider list with `'GB'` on attempt 2.
- **The one miss is a silent wrong answer after a retry.** x06 ("heavy metal"): the first guesses
  (`LIKE '%Heavy%'`, `= 'heavy metal'`) returned 0 and were sent back; the retry `LIKE '%metal%'`
  also counted the separate `'Metal'` style and returned 402 instead of 28. A non-zero count is
  not "suspicious", so nothing flagged it. The answer step even said the count "may include more
  than just heavy metal", but that caveat is not scored.
- **x04 was guessed without a retry:** both models wrote `LIKE '%hip%' OR LIKE '%rap%'`, which
  happens to match only `'Urban / Rap'`.
- **The real weakness is the schema summary, not the model.** A lookup table where every row is
  distinct (25 styles, 5 file formats) gets only 3 example values, so the model never sees
  `'Metal (Classic)'` or `'m4v-drm'`. Listing every value of small lookup tables would likely fix
  x06; I left the summary unchanged so the two eval sets use the same pipeline.

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

- **The first benchmark is saturated.** 40/40 for both models means that question set does not
  separate them and barely exercises the retry loop. Chinook is one of the most widely used
  sample databases, and the bare-schema ablation suggests the models either know its values or
  hedge well enough that it does not matter. The disguised set fixes the memorisation part, but
  at 19/20 each it still does not separate Sonnet 5.5 from Haiku 5.5; my 20 questions were written
  by someone who had read the schema, and a set that separates the models probably needs a larger
  unfamiliar schema and questions written blind.
- **The answer step invents a currency.** The disguised prices have no currency, but both models'
  answers for x05 say "GBP 8,464.50" (written with a pound sign) because the question mentions the UK.
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
  `web/data/runs.json`, written by `sqlask-eval` from real CLI calls (188 calls in total, cached
  in `.cache/`, so rerunning the eval costs nothing).

## Limitations

- SQLite only; the prompt, guard and date handling assume SQLite syntax.
- The guard is a keyword check, not a parser; the read-only connection is the real boundary.
- The in-browser database runs in a Web Worker and a query is stopped after 8 seconds; the
  Python side stops queries after 5 seconds.
- The answer text is not scored, and it can contain errors (see above).
- The recorded runs went through the Claude Code CLI, not the API directly; costs are what the
  CLI reported, and latency includes CLI start-up.
- 40 easy questions plus 20 harder ones on one database (and its disguised copy) is a small
  benchmark; one question is 5 points of accuracy.

## Layout

```
src/sqlask/guard.py      read-only guard (masking, keyword checks, LIMIT)
src/sqlask/db.py         read-only connection, authorizer, time limit
src/sqlask/schema.py     schema summary for the prompt (full or bare)
src/sqlask/providers.py  AnthropicProvider, ClaudeCLIProvider, FakeProvider
src/sqlask/pipeline.py   ask(): attempts, retry feedback, answer step
src/sqlask/scoring.py    execution-accuracy comparison
src/sqlask/evaluate.py   eval runner (sqlask-eval), writes web/data/runs.json
src/sqlask/disguise.py   builds the disguised copy (sqlask-disguise)
eval/questions.json      40 scored questions with gold SQL (Chinook)
eval/hard_questions.json 20 harder questions with gold SQL (disguised Chinook)
eval/demo_questions.json 4 unscored demo prompts
tests/                   pytest suite (offline) + guard_cases.json shared with the JS port
web/                     static demo: index.html, app.js, style.css, data/
```
