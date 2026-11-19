from sqlask.scoring import results_match


def test_order_insensitive_by_default():
    assert results_match([("a",), ("b",)], [("b",), ("a",)])
    assert not results_match([("a",), ("b",)], [("b",), ("a",)], ordered=True)


def test_numeric_normalisation():
    assert results_match([("2011", 469.58)], [(2011, 469.5800000001)])
    assert results_match([(1,)], [(1.0,)])
    assert not results_match([(35.5,)], [(35.0,)])


def test_extra_columns_allowed_but_not_in_strict():
    gold = [("Iron Maiden",)]
    pred = [(90, "Iron Maiden", 138.6)]
    assert results_match(gold, pred)
    assert not results_match(gold, pred, strict=True)


def test_column_permutation():
    assert results_match([("Rock", 1297), ("Jazz", 130)], [(130, "Jazz"), (1297, "Rock")])


def test_name_join_tolerance():
    gold = [("Nancy", "Edwards", "Andrew", "Adams"), ("Jane", "Peacock", "Nancy", "Edwards")]
    pred = [("Nancy Edwards", "Andrew Adams"), ("Jane Peacock", "Nancy Edwards")]
    assert results_match(gold, pred)
    assert not results_match(gold, pred, strict=True)


def test_row_count_must_match():
    assert not results_match([("a",)], [("a",), ("a",)])
    assert results_match([], [])
    assert not results_match([], [("a",)])


def test_duplicates_matter():
    assert not results_match([("Music",), ("Music",)], [("Music",)])
