"""Thin web viewer rendering + read-only guard."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ccq import server

if TYPE_CHECKING:
    import duckdb


def test_dashboard_renders_sections(con: duckdb.DuckDBPyConnection) -> None:
    body = server._dashboard(con, None)
    assert "Cost by model" in body
    assert "Top projects by cost" in body
    assert "demo-app" in body  # a real project from the fixture


def test_dashboard_runs_read_only_query(con: duckdb.DuckDBPyConnection) -> None:
    body = server._dashboard(con, "SELECT project FROM sessions ORDER BY 1")
    assert "Result (2 rows)" in body
    assert "web-api" in body


def test_dashboard_refuses_write(con: duckdb.DuckDBPyConnection) -> None:
    body = server._dashboard(con, "DROP VIEW sessions")
    assert "Refused" in body


def test_dashboard_escapes_and_reports_sql_error(con: duckdb.DuckDBPyConnection) -> None:
    body = server._dashboard(con, "SELECT * FROM nonexistent_table")
    assert "err" in body  # error class rendered, page does not crash


def test_dashboard_section_failure_is_isolated(con: duckdb.DuckDBPyConnection) -> None:
    # If one section's relation is gone, the page still renders the others.
    con.execute("DROP VIEW agents")
    body = server._dashboard(con, None)
    assert "Cost by model" in body  # a healthy section still rendered
    assert "err" in body  # the broken section degraded to an inline error
    assert "Top projects by cost" in body


def test_session_view_renders_timeline(con: duckdb.DuckDBPyConnection) -> None:
    body = server._session_view(con, "aaaa1111")
    assert "Timeline" in body
    assert "tool:Bash" in body
    assert "ERROR 429" in body
    assert "demo-app" in body


def test_session_view_missing_prefix(con: duckdb.DuckDBPyConnection) -> None:
    body = server._session_view(con, "zzzzzzzz")
    assert "No session matching" in body


def test_session_view_ambiguous_prefix(con: duckdb.DuckDBPyConnection) -> None:
    body = server._session_view(con, "")  # empty prefix matches both fixtures
    assert "ambiguous" in body


def test_project_panel(con: duckdb.DuckDBPyConnection) -> None:
    body = server._project_panel(con, "demo-app")
    assert "project: demo-app" in body
    assert "Cost by model" in body
    assert "Top tools" in body
    assert "/session?id=" in body  # session ids are drill-down links


def test_dashboard_project_param_routes_to_panel(con: duckdb.DuckDBPyConnection) -> None:
    body = server._dashboard(con, None, "web-api")
    assert "project: web-api" in body


def test_project_chips_links(con: duckdb.DuckDBPyConnection) -> None:
    chips = server._project_chips(con)
    assert "/?project=" in chips
    assert "demo-app" in chips


def test_table_link_renders_anchor() -> None:
    out = server._table(["id", "n"], [("abc123", 5)], link="/session?id={}")
    assert "<a href='/session?id=abc123'>abc123</a>" in out


def test_numeric_column_detection() -> None:
    assert server._is_num_col("cost_usd")
    assert server._is_num_col("out_tok")
    assert server._is_num_col("calls")
    assert not server._is_num_col("project")
    assert not server._is_num_col("model")
