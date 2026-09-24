"""kdbai `utils/filters.py` — client/LLM-supplied filter parsing on the live query path.

Crashes here don't escape: every call site in `kdbai_data.py` wraps `parse_temporal_filters` in a
broad `try/except` returning a structured error, so malformed input degrades cleanly rather than
taking the tool down. The real risk this file guards against is different and worse — **silent
wrong results**: an untested recursive case (a nested `and`/`or`/`not`, a `within` pair) could be
mis-parsed and still return `status: success` with incorrectly filtered data, no error, no signal,
on a tool an agent trusts. So these tests assert the actual OUTPUT STRUCTURE of the recursion, not
merely the absence of an exception.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone

import pytest

from kx_mcp_kdbai.utils.filters import (
    cast_temporal_value,
    is_list_of_iso_datetimes,
    is_nested_filter,
    parse_temporal_filters,
)

SCHEMA = [
    {"name": "ts", "type": "datetime64[ns]"},
    {"name": "d", "type": "date"},
    {"name": "t", "type": "time"},
    {"name": "sym", "type": "symbol"},
]


# --- is_nested_filter: the recursion-vs-value-list dispatch --------------------------------------


def test_is_nested_filter_recognises_every_documented_operator():
    for op in ["and", "or", "not", "=", "<>", "<", ">", "<=", ">=", "in", "like", "within", "fuzzy"]:
        assert is_nested_filter([op, "x", "y"]) is True


def test_is_nested_filter_rejects_a_plain_value_list():
    # e.g. an "in" operator's right-hand side: a list of values, not an operator expression.
    assert is_nested_filter(["AAPL", "MSFT"]) is False
    assert is_nested_filter([1, 2, 3]) is False


def test_is_nested_filter_rejects_non_lists_and_too_short_lists():
    assert is_nested_filter("not-a-list") is False
    assert is_nested_filter(42) is False
    assert is_nested_filter(["and"]) is False  # len < 2
    assert is_nested_filter([]) is False


# --- base case: non-list passthrough -------------------------------------------------------------


def test_non_list_filters_pass_through_unchanged():
    assert parse_temporal_filters(None, SCHEMA) is None
    assert parse_temporal_filters("literal", SCHEMA) == "literal"
    assert parse_temporal_filters(42, SCHEMA) == 42


def test_a_plain_value_list_on_the_right_is_left_unchanged():
    # e.g. an "in" filter's value list: neither a list of ISO datetimes (is_list_of_iso_datetimes)
    # nor a nested operator expression (is_nested_filter) -> right falls through untouched. This is
    # a common, real filter shape (`in`/`fuzzy` over a set of plain values) with no prior coverage.
    result = parse_temporal_filters([["in", "sym", ["AAPL", "MSFT"]]], SCHEMA)
    assert result == [["in", "sym", ["AAPL", "MSFT"]]]


def test_a_malformed_filter_item_is_passed_through_not_recursed():
    # len(f) not in {2, 3}: the ONLY branch whose comment ("recurse on every item") does not match
    # its own code (a plain `result.append(f)`, no recursion at all). Pinning current behavior —
    # any temporal value nested inside such an item is silently NOT cast, no error, no signal, which
    # is exactly the "returns success with unconverted/uncast data" risk this file's docstring
    # describes. Likely unreachable from the real kdbai filter DSL (every real filter is a 2- or
    # 3-item list) — kept as a documented pin, not a claim that this path is exercised in production.
    four_item = ["and", "extra", "items", "here"]
    result = parse_temporal_filters([four_item], SCHEMA)
    assert result == [four_item]

    one_item = ["lonely"]
    result = parse_temporal_filters([one_item], SCHEMA)
    assert result == [one_item]


@pytest.mark.parametrize("bad_item", ["abc", {"x": 1, "y": 2, "z": 3}])
def test_three_length_non_list_filter_item_is_rejected_not_silently_unpacked(bad_item):
    """A 3-length filter item that ISN'T a list (a 3-char string, a 3-key dict) is silently
    unpacked today via `op, left, right = f` — producing nonsense filters with no validation
    error. Must raise, not silently reinterpret."""
    with pytest.raises((TypeError, ValueError)):
        parse_temporal_filters([bad_item], SCHEMA)


# --- happy path: one comparison per temporal type ------------------------------------------------


def test_datetime_column_comparison_casts_to_datetime():
    # "Z" -> "+00:00" before fromisoformat, so the result is tz-AWARE (UTC), not naive.
    result = parse_temporal_filters([["<", "ts", "2025-01-01T00:00:00Z"]], SCHEMA)
    assert result == [["<", "ts", datetime(2025, 1, 1, tzinfo=timezone.utc)]]


def test_date_column_comparison_casts_to_date():
    result = parse_temporal_filters([["=", "d", "2025-06-15T00:00:00Z"]], SCHEMA)
    assert result == [["=", "d", date(2025, 6, 15)]]


def test_time_column_comparison_casts_to_time():
    # time.fromisoformat accepts the "Z" suffix directly (no "+00:00" replacement needed here) and
    # likewise returns a tz-AWARE time.
    result = parse_temporal_filters([["=", "t", "2025-06-15T12:30:00Z"]], SCHEMA)
    assert result == [["=", "t", time(12, 30, 0, tzinfo=timezone.utc)]]


def test_untyped_column_defaults_to_datetime64_ns():
    # col=None (no left-hand column name — e.g. a bare value comparison) defaults to
    # "datetime64[ns]", per cast_temporal_value's documented fallback.
    assert cast_temporal_value(None, "2025-01-01T00:00:00Z", SCHEMA) == datetime(2025, 1, 1, tzinfo=timezone.utc)


def test_non_temporal_column_is_left_unchanged():
    # "sym" is not a datetime-like type -> cast_temporal_value is a no-op.
    result = parse_temporal_filters([["=", "sym", "AAPL"]], SCHEMA)
    assert result == [["=", "sym", "AAPL"]]


def test_unknown_column_is_left_unchanged():
    # type_map.get("ghost") -> None -> not in datetime_types -> passthrough, not a KeyError.
    result = parse_temporal_filters([["=", "ghost", "2025-01-01T00:00:00Z"]], SCHEMA)
    assert result == [["=", "ghost", "2025-01-01T00:00:00Z"]]


# --- within: a list of ISO datetimes on the right-hand side ---------------------------------------


def test_within_pair_casts_both_bounds():
    result = parse_temporal_filters(
        [["within", "ts", ["2025-01-01T00:00:00Z", "2025-02-01T00:00:00Z"]]], SCHEMA
    )
    assert result == [
        ["within", "ts", [datetime(2025, 1, 1, tzinfo=timezone.utc), datetime(2025, 2, 1, tzinfo=timezone.utc)]]
    ]


def test_is_list_of_iso_datetimes_true_for_a_valid_pair():
    assert is_list_of_iso_datetimes(["2025-01-01T00:00:00Z", "2025-02-01T00:00:00Z"]) is True


def test_is_list_of_iso_datetimes_false_for_non_datetime_strings():
    assert is_list_of_iso_datetimes(["AAPL", "MSFT"]) is False


def test_is_list_of_iso_datetimes_false_for_non_string_items():
    assert is_list_of_iso_datetimes([1, 2, 3]) is False


def test_is_list_of_iso_datetimes_false_for_a_non_list():
    assert is_list_of_iso_datetimes("2025-01-01T00:00:00Z") is False


# --- nested boolean recursion: assert STRUCTURE, not just absence of error -----------------------


def test_and_recurses_into_both_sides_independently():
    # Only the temporal side should be cast; the symbol comparison must survive untouched.
    result = parse_temporal_filters(
        [["and", ["<", "ts", "2025-01-01T00:00:00Z"], ["=", "sym", "AAPL"]]], SCHEMA
    )
    assert result == [["and", ["<", "ts", datetime(2025, 1, 1, tzinfo=timezone.utc)], ["=", "sym", "AAPL"]]]


def test_or_recurses_into_both_sides():
    result = parse_temporal_filters(
        [["or", ["=", "sym", "AAPL"], ["=", "sym", "MSFT"]]], SCHEMA
    )
    assert result == [["or", ["=", "sym", "AAPL"], ["=", "sym", "MSFT"]]]


def test_not_unary_recurses_into_its_single_operand():
    result = parse_temporal_filters(
        [["not", ["=", "ts", "2025-01-01T00:00:00Z"]]], SCHEMA
    )
    assert result == [["not", ["=", "ts", datetime(2025, 1, 1, tzinfo=timezone.utc)]]]


def test_deeply_nested_and_or_not_all_cast_correctly():
    # and[ or[ ts<A, ts>B ], not[ sym=AAPL doesn't matter, but structure must survive] ]
    query = [
        "and",
        ["or", ["<", "ts", "2025-01-01T00:00:00Z"], [">", "ts", "2025-06-01T00:00:00Z"]],
        ["not", ["=", "sym", "AAPL"]],
    ]
    result = parse_temporal_filters([query], SCHEMA)
    assert result == [
        [
            "and",
            [
                "or",
                ["<", "ts", datetime(2025, 1, 1, tzinfo=timezone.utc)],
                [">", "ts", datetime(2025, 6, 1, tzinfo=timezone.utc)],
            ],
            ["not", ["=", "sym", "AAPL"]],
        ]
    ]


def test_multiple_top_level_filters_each_parsed_independently():
    result = parse_temporal_filters(
        [["=", "sym", "AAPL"], ["<", "ts", "2025-01-01T00:00:00Z"]], SCHEMA
    )
    assert result == [["=", "sym", "AAPL"], ["<", "ts", datetime(2025, 1, 1, tzinfo=timezone.utc)]]
