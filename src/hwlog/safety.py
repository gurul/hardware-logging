"""Shared input limits for user/model-controlled operations."""

from __future__ import annotations

import math
import re as stdlib_re
from typing import Any

import regex

MAX_PATTERN_CHARS = 512
MAX_REPEAT_COUNT = 1_000
MAX_TOTAL_REPEAT_COUNT = 10_000
MAX_REPEAT_COMPLEXITY = 10_000
MAX_WAIT_SECONDS = 120.0
REGEX_SEARCH_TIMEOUT_SECONDS = 0.05


class PatternError(ValueError):
    """A pattern is invalid, too large, or too expensive to evaluate safely."""


_COUNTED_REPEAT = stdlib_re.compile(r"\s*(\d+)\s*(?:,\s*(\d*)\s*)?")


def _validate_counted_repeats(pattern: str) -> None:
    """Reject repeat counts that make regex compilation expand excessively."""
    repeats: list[tuple[int, int, int]] = []
    for index, char in enumerate(pattern):
        if char != "{":
            continue
        preceding_slashes = 0
        cursor = index - 1
        while cursor >= 0 and pattern[cursor] == "\\":
            preceding_slashes += 1
            cursor -= 1
        if preceding_slashes % 2:
            continue
        closing = pattern.find("}", index + 1)
        if closing < 0:
            continue
        match = _COUNTED_REPEAT.fullmatch(pattern[index + 1 : closing])
        if match is None:
            continue
        counts = [int(value) for value in match.groups() if value]
        if any(count > MAX_REPEAT_COUNT for count in counts):
            raise PatternError(f"regex repeat count exceeds {MAX_REPEAT_COUNT}")
        repeats.append((index, closing, max(counts)))

    counts = [count for _, _, count in repeats]
    if sum(counts) > MAX_TOTAL_REPEAT_COUNT:
        raise PatternError("regex aggregate repeat count is too large")
    complexity = 1
    for count in counts:
        complexity *= max(1, count)
        if complexity > MAX_REPEAT_COMPLEXITY:
            raise PatternError("combined regex repeat complexity is too large")


def compile_pattern(pattern: str) -> Any:
    """Compile a bounded regex using an engine with per-search timeouts."""
    if not isinstance(pattern, str):
        raise PatternError("pattern must be a string")
    if len(pattern) > MAX_PATTERN_CHARS:
        raise PatternError(f"pattern exceeds {MAX_PATTERN_CHARS} characters")
    _validate_counted_repeats(pattern)
    try:
        return regex.compile(pattern)
    except regex.error as exc:
        raise PatternError(f"invalid regex: {exc}") from exc


def pattern_matches(compiled: Any, text: str) -> bool:
    """Search with a hard per-record runtime bound."""
    try:
        return compiled.search(text, timeout=REGEX_SEARCH_TIMEOUT_SECONDS) is not None
    except TimeoutError as exc:
        raise PatternError("regex exceeded the per-record execution limit") from exc


def bounded_timeout(value: float, maximum: float = MAX_WAIT_SECONDS) -> float:
    """Validate a timeout and cap it to the public maximum."""
    try:
        timeout = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("timeout must be a number") from exc
    if not math.isfinite(timeout) or timeout < 0:
        raise ValueError("timeout must be a finite, non-negative number")
    return min(timeout, maximum)
