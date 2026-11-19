# Code review repairs, 2026-11-19

Source: `../_portfolio-shared/qc-sqlask.md`. The interrupted Python changes were
inspected and retained where correct. No Claude CLI or model API was called.

| Finding | Status | Repair and regression coverage |
|---|---|---|
| 1. Empty results inflate strict accuracy | Fixed | Both scorers accept column metadata and validate empty-result shape. Shared equal/unequal-column fixtures and recorded h02 results cover it. |
| 2. Failed retries return stale success | Fixed | Provisional empty results are separate from accepted results. SQL errors, guard failures, and provider errors after SELECT 0 now fail without returning stale rows. |
| 3. Incomplete answers receive credit | Fixed | Python evaluation and browser verification compare complete model/gold results independently of the 200-row display cap. Both fetch an extra row beyond the 100,000-row evaluation limit to detect truncation. Prefix-only answers fail; over-limit predictions are inconclusive and receive no credit. |
| 4. Rounding differs across languages | Fixed | JavaScript rounds the exact binary64 value to two decimal places, with ties to even, matching Python. Shared fixtures cover positive/negative ties, numeric strings, and nearby floats. |
| 5. Cancelled replay overwrites selection | Fixed | Completion, error, and button callbacks check cancellation. Tests resolve overlapping replays in reverse order and check stale failures and button state. |
| 6. Old timeout kills replacement worker | Fixed | Requests belong to their worker instance. A restart clears timers and rejects every request owned by that worker; stale callbacks cannot affect its replacement. |
| 7. Empty confirmation changes literals | Fixed | SQL normalization preserves quoted strings and identifiers. Case, repeated spaces, and escaped quotes inside literals no longer confirm a changed query. |
| 8. SQL used differs from execution | Fixed | Results and answer prompts use executed SQL, with model SQL retained separately. Added, capped, and wrapped LIMIT cases are compared with SQLite's execution trace. Browser verification displays its actual guarded SQL. |
| 9. Relative database paths crash | Fixed | Database, schema, and disguise access resolve paths before constructing encoded read-only URIs. Tests exercise relative names containing spaces, question marks, and hashes, including the CLI with a fake provider. |
| 10. Unicode guard parity fails | Fixed | Both guards use SQLite identifier boundaries, whitespace, and ASCII keyword case folding. Shared fixtures include Unicode and dollar-prefixed identifiers around forbidden words and LIMIT. |
| 11. Page overstates live replay | Fixed | Attempts are labeled recorded playback; only final/gold SQL verification is described as live. A multi-attempt replay test counts exactly two executions and checks the wording. |

## Published numbers

Recomputed from the recorded replies and SQLite databases in `web/data`:

| Published value | Before | After |
|---|---|---|
| Sonnet original strict | 27/40 (67.5%) | 26/40 (65.0%) |
| Haiku original strict | 26/40 (65.0%) | 25/40 (62.5%) |
| Shared guard fixtures in README | 28 | 35 |
| Original-suite accuracy change per question | 5 percentage points | 2.5 percentage points; harder suite remains 5 |
| README fixed test count | 64 | Removed; the verified suite currently has 127 tests |
| Estimated schema token count | Approximately 2,000 | Removed; token counts were not retained in the recordings |

New documentation states the implemented comparison limits: 100,000 rows and
20 seconds in Python; browser verification retains its 8-second timeout. The
CLI example now includes the actual LIMIT 200. All other recorded benchmark
counts, medians, costs, and call totals are unchanged, including 188 total calls
and 49 calls for the two disguised sets. Original SQL replies, attempt metadata,
answer text, and per-run costs were checked against the original commit and
preserved exactly.

## Verification and reproduction

- New regressions were run against an isolated copy of the original commit:
  32 failures and 49 passes across the review/guard tests, covering all eleven
  findings. The active working tree was not reset.
- `.venv/bin/python -m pytest -q -p no:cacheprovider`: 127 passed.
- `node --check web/app.js` and `git diff --check`: passed.
- `.venv/bin/python -m sqlask.replay --check`: no outcome changed after rescoring.
- All 126 scored recordings agree with the corrected Python and JavaScript
  scorers. Tests also recompute every stored summary and check README result
  tables, medians, costs, and call totals against the recordings.
- Browser regressions run the real application code in Node with controlled
  DOM, worker, and timer substitutes. A full browser/CDN/sql.js smoke run was
  not performed.

The primary Obsidian vault was readable, but this session does not allow writes
to it. This repository report retains the project result for later vault filing.

Local commit is pending: `git add` failed because the sandbox denied creating
`.git/index.lock` (Operation not permitted). No commit or push was made. Once
Git metadata is writable, stage the new files and run:

```sh
git add src/sqlask/replay.py tests/browser_regressions.js tests/scoring_cases.json tests/test_recordings.py tests/test_review_regressions.py docs/review-fixes.md
git -c user.name=mridulsn -c user.email=mridul8527@gmail.com commit -am 'Fix review findings and recompute recorded evaluation scores'
```
