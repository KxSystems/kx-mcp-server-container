from typing import List, Any
from datetime import datetime, date, time

def is_nested_filter(item):
    if not isinstance(item, list) or len(item) < 2:
        return False
    return item[0] in ['and', 'or', 'not', '=', '<>', '<', '>', '<=', '>=', 'in', 'like', 'within', 'fuzzy']


# function to convert ISO format datetime strings to correct python object as per table schema
# `filters` is walked recursively and may be None, a list of lists, or a list of tuples — non-list
# inputs are returned as-is (base case below), so the honest annotation is Any, not List[List].
def parse_temporal_filters(filters: Any, schema: List[dict]) -> Any:
    if not isinstance(filters, list):
        return filters  # Base case
    # Annotated: the pass-through branch appends the item verbatim, which may be a tuple, so the
    # element type is deliberately open rather than inferred from the rewritten-filter branch.
    result: list = []
    for f in filters:
        if not isinstance(f, (list, tuple)):
            # A filter ITEM must be a sequence. Without this, `len(f)` on a 3-character string is 3,
            # so `op, left, right = f` unpacked it into three characters and emitted a nonsense
            # filter with no error at all (a 3-key dict unpacked into its KEYS, just as quietly).
            # Note this rejects by TYPE only: an unexpected LENGTH still passes through untouched
            # (see the `else` branch), which is the documented behaviour pinned by
            # test_a_malformed_filter_item_is_passed_through_not_recursed.
            raise TypeError(
                f"filter item must be a list or tuple, got {type(f).__name__}: {f!r}"
            )
        if len(f) == 3:
            # Could be an operator like ["or", [...], [...]] or a comparison like ["<", "time", "2025-01-01..."]
            op, left, right = f
            col = None if isinstance(left, list) else left
            left = parse_temporal_filters([left], schema)[0] if isinstance(left, list) else left
            if isinstance(right, list):
                if is_list_of_iso_datetimes(right):
                   right= [cast_temporal_value(col, val, schema) for val in right]
                elif is_nested_filter(right):
                    right = parse_temporal_filters([right], schema)[0]
            else:
                right = cast_temporal_value(col, right, schema)
            result.append([op, left, right])

        elif len(f) == 2:
            # Unary operation like ["not", [...]]
            op, inner = f
            result.append([op, parse_temporal_filters([inner], schema)[0]])

        else:
            # An unexpected LENGTH (not 2 or 3) passes through verbatim. Note this does NOT recurse,
            # despite what the comment here used to claim — so any temporal value nested inside such
            # an item is silently not cast. Left as-is deliberately: every filter the real kdbai DSL
            # produces is a 2- or 3-item list, and tightening the length rule is a separate,
            # behaviour-changing decision from rejecting an outright wrong TYPE above.
            result.append(f)

    return result

# checks if string is iso datetime format or not
def is_list_of_iso_datetimes(lst):
    if not isinstance(lst, list):
        return False
    try:
        for item in lst:
            if not isinstance(item, str):
                return False
            datetime.fromisoformat(item.replace("Z", "+00:00"))  # handles 'Z' for UTC
        return True
    except Exception:
        return False

# converts ISO datetime string to correct python type
def cast_temporal_value(col, val, schema):
    # Create a mapping from column name to column type
    type_map = {col["name"]: col["type"] for col in schema}
    # Known datetime-like types
    datetime_types = {"datetime", "datetime64[ns]", "date", "time"}
    if col is None:
        field_type = "datetime64[ns]" # default type
    else:
        field_type = type_map.get(col)
    result = val
    if field_type in datetime_types:
        if field_type in {"datetime", "datetime64[ns]"}:
            result = datetime.fromisoformat(val.replace("Z", "+00:00"))
        elif field_type == "date":
            result = date.fromisoformat(val.split("T")[0])
        elif field_type == "time":
            # Normalise the 'Z' UTC suffix exactly as the datetime branch above does (KXI-72920).
            # `time.fromisoformat` only learned to parse a bare 'Z' in 3.11, so without this a
            # value like "2025-06-15T12:30:00Z" raised `ValueError: Invalid isoformat string:
            # '12:30:00Z'` at this repo's declared 3.10 floor. kdbai_data.py's broad try/except
            # swallowed it into a clean `status: error`, so the query silently didn't run.
            result = time.fromisoformat(val.split("T")[1].replace("Z", "+00:00"))
    return result