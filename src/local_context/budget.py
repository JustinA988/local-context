"""Size caps so a tool result cannot grow the context window without a bound."""

from __future__ import annotations

MIN_OUTPUT_CHARS = 500
MAX_OUTPUT_CHARS = 16_000
DEFAULT_OUTPUT_CHARS = 4_000
MAX_COMMAND_TIMEOUT = 120
DEFAULT_COMMAND_TIMEOUT = 20
MAX_ROWS = 100
DEFAULT_ROWS = 25
MAX_CAPTURE_CHARS = 64_000
MAX_READ_OFFSET = 200_000
MAX_COMMAND_CHARS = 4_000


def clamp_int(value: object, default: int, low: int, high: int) -> int:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    if number < low:
        return low
    if number > high:
        return high
    return number


def fit(header: str, body: str, limit: int, start_offset: int) -> str:
    """Return header + body, clipped so the result is at most `limit` characters.

    When body is clipped, the notice advertises `offset=<start_offset + shown>`.
    """
    if limit < 1:
        limit = 1
    if len(header) >= limit:
        return header[:limit]
    budget = limit - len(header)
    if len(body) <= budget:
        return header + body
    keep = min(len(body), budget)
    while True:
        omitted = len(body) - keep
        notice = f"\n[truncated {omitted} chars; offset={start_offset + keep}]"
        if len(notice) <= budget and keep + len(notice) <= budget:
            return header + body[:keep] + notice
        if keep == 0:
            return (header + notice)[:limit]
        keep -= 1


def page(text: str, offset: int, limit: int) -> str:
    """Return a window of `text` starting at a character offset, capped at `limit`."""
    if limit < 1:
        limit = 1
    if offset < 0:
        offset = 0
    if offset > len(text):
        return f"[offset {offset} past end {len(text)}]"[:limit]
    prefix = f"[offset {offset}]\n" if offset else ""
    return fit(prefix, text[offset:], limit, offset)


def with_header(header: str, text: str, offset: int, limit: int) -> str:
    if len(header) >= limit:
        return header[:limit]
    return header + page(text, offset, limit - len(header))
