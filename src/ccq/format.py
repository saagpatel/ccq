"""Render query results as an aligned table, JSON, or CSV."""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
from collections.abc import Sequence

Row = Sequence[object]


def _scalar(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, dt.datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    if isinstance(value, float):
        return f"{value:,.2f}"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value)


def _json_safe(value: object) -> object:
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    return value


def _csv_safe(value: object) -> object:
    # Machine-readable: raw numbers (no thousands separators), ISO datetimes.
    if value is None:
        return ""
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return value  # int/float/str pass through; csv.writer stringifies plainly


def render(columns: list[str], rows: Sequence[Row], fmt: str = "table") -> str:
    """Format ``rows`` under ``columns`` in the requested output format."""
    if fmt == "json":
        return json.dumps(
            [{c: _json_safe(v) for c, v in zip(columns, r, strict=False)} for r in rows],
            indent=2,
        )
    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(columns)
        for r in rows:
            writer.writerow([_csv_safe(v) for v in r])
        return buf.getvalue().rstrip("\n")
    return _table(columns, rows)


def _table(columns: list[str], rows: Sequence[Row]) -> str:
    if not rows:
        return "(no rows)"
    cells = [[_scalar(v) for v in r] for r in rows]
    widths = [len(c) for c in columns]
    for row in cells:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    # Right-align numeric-looking columns, left-align the rest.
    numeric = [all(_looks_numeric(row[i]) for row in cells if row[i]) for i in range(len(columns))]

    def fmt_row(values: list[str]) -> str:
        out = []
        for i, v in enumerate(values):
            out.append(v.rjust(widths[i]) if numeric[i] else v.ljust(widths[i]))
        return "  ".join(out).rstrip()

    header = fmt_row(list(columns))
    rule = "  ".join("-" * w for w in widths)
    body = "\n".join(fmt_row(row) for row in cells)
    return f"{header}\n{rule}\n{body}"


def _looks_numeric(text: str) -> bool:
    return bool(text) and all(ch.isdigit() or ch in ",.-" for ch in text)
