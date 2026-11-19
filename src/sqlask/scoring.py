"""Execution accuracy: does the predicted result equal the gold result?

Rules (written down so the score can be argued with):
- Values are normalised: numbers (and numeric strings like '2011') become
  floats rounded to 2 decimals; text is compared exactly; NULL equals NULL.
  Rounding is Python's round(x, 2): correctly rounded from the exact binary
  value, ties to even (1.125 -> 1.12, 0.375 -> 0.38, 2.675 -> 2.67 because the
  stored double is just below 2.675). web/app.js implements the same rule and
  both are checked against tests/scoring_cases.json.
- Rows are compared as a multiset, or as a sequence when the question asks
  for an order (`ordered: true`).
- Column names and column order do not matter. The prediction may carry
  extra columns (e.g. an id or a count next to the name that was asked for):
  every gold column must map to a distinct predicted column.
- One join tolerance: two adjacent gold text columns (FirstName, LastName)
  also match a single predicted column holding "First Last" (any number of
  such pairs, e.g. employee name + manager name).
- Shape counts even with no rows: pass the column counts (gold_ncols,
  pred_ncols) and an empty prediction only matches an empty gold result when
  it could carry the gold columns (strict: the same number of columns;
  otherwise at least as many as gold has after joining adjacent pairs).
`strict` mode turns off the extra-column and name-join tolerances.
"""
from __future__ import annotations

from collections import Counter
from decimal import Decimal
from itertools import product
import re

_NUMERIC = re.compile(r"^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$")


def norm(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (int, float, Decimal)):
        return round(float(v), 2) + 0.0
    if isinstance(v, str):
        s = v.strip()
        if _NUMERIC.match(s):  # stricter than float(): no 'nan' / 'inf' / '1_000'
            return round(float(s), 2) + 0.0
        return s
    return str(v)


def _rows_equal(gold: list[tuple], pred: list[tuple], ordered: bool) -> bool:
    if len(gold) != len(pred):
        return False
    if ordered:
        return gold == pred
    return Counter(gold) == Counter(pred)


def _gold_views(gold_rows: list[tuple]) -> list[list[tuple]]:
    """The gold rows as-is, plus every version where one or more
    non-overlapping adjacent text pairs are joined with a space."""
    if not gold_rows:
        return [gold_rows]
    ncols = len(gold_rows[0])
    pairs = [i for i in range(ncols - 1)
             if all(isinstance(r[i], str) and isinstance(r[i + 1], str) for r in gold_rows)]

    def subsets(start: int):
        yield []
        for i in pairs:
            if i >= start:
                for rest in subsets(i + 2):
                    yield [i] + rest

    views = []
    for chosen in subsets(0):
        rows = []
        for r in gold_rows:
            out, j = [], 0
            while j < ncols:
                if j in chosen:
                    out.append(f"{r[j]} {r[j + 1]}")
                    j += 2
                else:
                    out.append(r[j])
                    j += 1
            rows.append(tuple(out))
        views.append(rows)
    return views


def _empty_shape_ok(gold_ncols: int | None, pred_ncols: int | None, strict: bool) -> bool:
    """Both results are empty: compare their column counts (when known)."""
    if gold_ncols is None or pred_ncols is None:
        return True
    if strict:
        return gold_ncols == pred_ncols
    # the name-join tolerance can fold at most every second gold column
    return pred_ncols >= gold_ncols - gold_ncols // 2


def _match_view(gold: list[tuple], pred: list[tuple], ordered: bool, strict: bool) -> bool:
    if len(gold) != len(pred):
        return False
    if not gold:
        return True
    g_cols, p_cols = len(gold[0]), len(pred[0])
    if g_cols > p_cols or (strict and g_cols != p_cols):
        return False
    # candidate predicted columns for each gold column: same multiset of values
    g_col_vals = [Counter(r[i] for r in gold) for i in range(g_cols)]
    p_col_vals = [Counter(r[j] for r in pred) for j in range(p_cols)]
    cands = [[j for j in range(p_cols) if p_col_vals[j] == g_col_vals[i]] for i in range(g_cols)]
    if any(not c for c in cands):
        return False
    tried = 0
    for mapping in product(*cands):
        if len(set(mapping)) != g_cols:
            continue
        tried += 1
        if tried > 5000:
            break
        projected = [tuple(r[j] for j in mapping) for r in pred]
        if _rows_equal(gold, projected, ordered):
            return True
    return False


def results_match(gold_rows, pred_rows, ordered: bool = False, strict: bool = False,
                  gold_ncols: int | None = None, pred_ncols: int | None = None) -> bool:
    gold = [tuple(norm(v) for v in r) for r in gold_rows]
    pred = [tuple(norm(v) for v in r) for r in pred_rows]
    if not gold and not pred:
        return _empty_shape_ok(gold_ncols, pred_ncols, strict)
    views = [gold] if strict else _gold_views(gold)
    return any(_match_view(v, pred, ordered, strict) for v in views)
