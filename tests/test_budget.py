from __future__ import annotations

import pytest

from local_context.budget import fit, page, with_header


@pytest.mark.parametrize("limit", [80, 500, 4000])
@pytest.mark.parametrize("size", [0, 10, 79, 80, 81, 5000])
def test_page_stays_inside_the_cap(limit: int, size: int) -> None:
    text = "a" * size
    out = page(text, 0, limit)
    assert len(out) <= limit
    if size <= limit:
        assert out == text


def test_page_continuation_reaches_the_rest() -> None:
    text = "x" * 1000
    first = page(text, 0, 120)
    assert len(first) <= 120
    marker = "offset="
    assert marker in first
    offset = int(first.rsplit(marker, 1)[1].rstrip("]"))
    second = page(text, offset, 120)
    assert len(second) <= 120
    assert text[offset : offset + 10] in second


def test_with_header_counts_against_the_same_cap() -> None:
    header = "exit=0\n"
    body = "y" * 500
    out = with_header(header, body, 0, 100)
    assert out.startswith(header)
    assert len(out) <= 100


def test_fit_keeps_the_header() -> None:
    out = fit("HEAD\n", "z" * 50, 20, 0)
    assert out.startswith("HEAD\n")
    assert len(out) <= 20
