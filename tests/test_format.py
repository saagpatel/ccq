"""Output rendering: table (human) vs csv/json (machine)."""

from __future__ import annotations

import datetime as dt
import json

from ccq.format import render

_COLS = ["model", "calls", "cost_usd"]
_ROWS = [("claude-opus-4-8", 12345, 6789.42), ("claude-haiku-4-5", 1930, 46.08)]


def test_table_uses_human_thousands_separators() -> None:
    out = render(_COLS, _ROWS, "table")
    assert "12,345" in out  # human-readable grouping in the table view
    assert "6,789.42" in out


def test_csv_emits_raw_numbers_not_grouped() -> None:
    out = render(_COLS, _ROWS, "csv")
    lines = out.splitlines()
    assert lines[0] == "model,calls,cost_usd"
    assert lines[1] == "claude-opus-4-8,12345,6789.42"  # no commas-in-number, machine-parseable
    assert "12,345" not in out


def test_json_emits_native_types() -> None:
    parsed = json.loads(render(_COLS, _ROWS, "json"))
    assert parsed[0]["calls"] == 12345
    assert parsed[0]["cost_usd"] == 6789.42


def test_datetime_and_none_handling() -> None:
    cols = ["ts", "x"]
    rows = [(dt.datetime(2026, 6, 20, 13, 5), None)]
    assert "2026-06-20T13:05:00" in render(cols, rows, "csv")
    assert "2026-06-20T13:05:00" in render(cols, rows, "json")
    assert "2026-06-20 13:05" in render(cols, rows, "table")


def test_empty_rows() -> None:
    assert render(_COLS, [], "table") == "(no rows)"
    assert render(_COLS, [], "json") == "[]"
    assert render(_COLS, [], "csv").strip() == "model,calls,cost_usd"
