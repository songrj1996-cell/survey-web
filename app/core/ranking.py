"""Strict parsing and deterministic diagnostics for matrix ranking answers."""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any


_PLAIN_RANK_RE = re.compile(r"^([1-9]\d*)(?:\.0+)?$")
_PREFIXED_RANK_RE = re.compile(
    r"^(?:peringkat|rank)\s+([1-9]\d*)(?:\.0+)?$",
    re.IGNORECASE,
)
_CHINESE_RANK_RE = re.compile(r"^第\s*([1-9]\d*)\s*名$")


def parse_rank_value(value: Any, *, max_rank: int | None = None) -> int | None:
    """Parse only an explicit rank value; never extract digits from free text."""
    rank: int | None = None
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        rank = value
    elif isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            return None
        rank = int(value)
    else:
        text = str(value).strip()
        if not text:
            return None
        match = (
            _PLAIN_RANK_RE.fullmatch(text)
            or _PREFIXED_RANK_RE.fullmatch(text)
            or _CHINESE_RANK_RE.fullmatch(text)
        )
        if match:
            rank = int(match.group(1))

    if rank is None or rank < 1:
        return None
    if max_rank is not None and rank > max_rank:
        return None
    return rank


def diagnose_matrix_ranking(
    body: list[list],
    column_indexes: list[int],
    *,
    min_complete_rows: int = 5,
) -> dict:
    """Check whether every responding row is a complete ``1..N`` permutation.

    Entirely blank rows are treated as non-responses. Any partial, unparseable,
    duplicated, or out-of-range response blocks deterministic classification.
    """
    indexes = [index for index in column_indexes if isinstance(index, int)]
    width = len(indexes)
    expected = set(range(1, width + 1))
    reasons: Counter = Counter()
    observed_aliases: dict[int, list[str]] = {
        rank: [] for rank in range(1, width + 1)
    }
    observed_keys: dict[int, set[str]] = {
        rank: set() for rank in range(1, width + 1)
    }
    complete_rows = 0
    blank_rows = 0
    invalid_rows = 0

    if (
        width < 2
        or len(indexes) != len(column_indexes)
        or len(set(indexes)) != width
        or any(index < 0 for index in indexes)
    ):
        return {
            "eligible": False,
            "column_count": width,
            "complete_rows": 0,
            "blank_rows": 0,
            "invalid_rows": len(body),
            "invalid_reasons": {"invalid_columns": len(body) or 1},
            "options": [],
            "value_aliases": {},
        }

    for row in body:
        raw_values = [row[index] if index < len(row) else "" for index in indexes]
        texts = [str(value).strip() if value is not None else "" for value in raw_values]
        if not any(texts):
            blank_rows += 1
            continue
        if not all(texts):
            invalid_rows += 1
            reasons["missing"] += 1
            continue

        parsed = [parse_rank_value(value, max_rank=width) for value in raw_values]
        if any(rank is None for rank in parsed):
            invalid_rows += 1
            reasons["unparseable_or_out_of_range"] += 1
            continue
        if set(parsed) != expected or len(set(parsed)) != width:
            invalid_rows += 1
            reasons["not_complete_permutation"] += 1
            continue

        complete_rows += 1
        for raw, rank in zip(raw_values, parsed):
            alias = str(raw).strip()
            key = alias.casefold()
            if key not in observed_keys[rank]:
                observed_keys[rank].add(key)
                observed_aliases[rank].append(alias)

    options = [f"第{rank}名" for rank in range(1, width + 1)]
    value_aliases = {
        f"第{rank}名": observed_aliases[rank]
        for rank in range(1, width + 1)
        if observed_aliases[rank]
    }
    eligible = (
        invalid_rows == 0
        and complete_rows >= min_complete_rows
        and all(observed_aliases[rank] for rank in range(1, width + 1))
    )
    return {
        "eligible": eligible,
        "column_count": width,
        "complete_rows": complete_rows,
        "blank_rows": blank_rows,
        "invalid_rows": invalid_rows,
        "invalid_reasons": dict(reasons),
        "options": options,
        "value_aliases": value_aliases,
    }
