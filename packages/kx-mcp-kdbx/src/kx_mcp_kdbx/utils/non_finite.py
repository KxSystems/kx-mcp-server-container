"""Diagnose non-finite floats in q's `.j.j` output, for every caller that parses it.

q's `.j.j` renders +/-infinity as the bare tokens `inf` / `-inf`, which are NOT valid JSON, so
`json.loads` fails with a generic parse error that gives no hint the real cause is a non-finite
float rather than a malformed query. (A null float `0n` is fine — `.j.j` emits a proper JSON null.)

Shared so the SQL tool and the metadata preview name the same cause the same way: the SQL tool
refuses such a result as `non_finite_number`; the preview re-reads its rows with infinities
projected to null, and reports `non_finite_number` only if that still cannot be parsed.
"""

from __future__ import annotations

import re

NON_FINITE_ERROR_TYPE = "non_finite_number"

_NON_FINITE_JSON = re.compile(rb"(?<![\w.])-?(?:inf|infinity|nan)(?![\w.])", re.IGNORECASE)


def has_non_finite(payload: object) -> bool:
    """True if a `.j.j` payload that failed to parse carries a bare non-finite token.

    Only meaningful after `json.loads` has already failed: a symbol value such as `` `inf `` is
    emitted quoted and would match too, but then the payload parses and this is never asked.
    """
    return isinstance(payload, bytes) and _NON_FINITE_JSON.search(payload) is not None
