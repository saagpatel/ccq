"""View-level checks against the synthetic corpus."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    import duckdb


def _rows(con: duckdb.DuckDBPyConnection, sql: str) -> list[tuple[object, ...]]:
    return con.execute(sql).fetchall()


def test_sessions_and_project_mapping(con: duckdb.DuckDBPyConnection) -> None:
    rows = _rows(con, "SELECT project FROM sessions ORDER BY project")
    projects = [r[0] for r in rows]
    assert projects == ["demo-app", "web-api"]


def test_message_usage_cost(con: duckdb.DuckDBPyConnection) -> None:
    cost = con.execute(
        "SELECT round(sum(cost_usd), 6) FROM message_usage WHERE project = 'demo-app'"
    ).fetchone()[0]
    assert cost == pytest.approx(0.063125)


def test_session_cost_rolls_up(con: duckdb.DuckDBPyConnection) -> None:
    demo = con.execute(
        "SELECT round(cost_usd, 6) FROM sessions WHERE project = 'demo-app'"
    ).fetchone()
    assert demo[0] == pytest.approx(0.063125)


def test_tool_calls_counts(con: duckdb.DuckDBPyConnection) -> None:
    counts = dict(_rows(con, "SELECT tool_name, count(*) FROM tool_calls GROUP BY 1"))
    assert counts == {"Bash": 1, "Agent": 1, "Read": 1}


def test_agent_pairing_total_tokens(con: duckdb.DuckDBPyConnection) -> None:
    rows = _rows(con, "SELECT subagent_type, model, subagent_tokens FROM agents")
    assert rows == [("Explore", "haiku", 55000)]


def test_errors_detected(con: duckdb.DuckDBPyConnection) -> None:
    rows = _rows(con, "SELECT project, status FROM errors")
    assert rows == [("demo-app", "429")]


def test_prompts_searchable(con: duckdb.DuckDBPyConnection) -> None:
    hits = _rows(
        con,
        "SELECT session_id FROM prompts WHERE lower(text) LIKE '%duckdb%'",
    )
    # Both the typed prompt and the ai-title mention 'duckdb' for session A.
    assert {r[0] for r in hits} == {"aaaa1111-0000-0000-0000-000000000001"}
    assert len(hits) == 2


def test_empty_dir_yields_no_rows(tmp_path: object) -> None:
    from pathlib import Path  # noqa: PLC0415

    from ccq.db import connect  # noqa: PLC0415

    empty = Path(str(tmp_path)) / "projects"
    empty.mkdir()
    con = connect(empty)
    assert con.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0
    con.close()


def test_sql_cost_uses_tier_fallback_for_unlisted_model(tmp_path: object) -> None:
    import json  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    from ccq.db import connect  # noqa: PLC0415

    base = Path(str(tmp_path)) / "projects"
    proj = base / "-home-user-Projects-x"
    proj.mkdir(parents=True)
    turn = {
        "type": "assistant",
        "sessionId": "s1",
        "cwd": "/home/user/Projects/x",
        "timestamp": "2026-06-01T00:00:00.000Z",
        "message": {
            "role": "assistant",
            "model": "claude-3-5-sonnet-20241022",  # unlisted, date-stamped
            "usage": {
                "input_tokens": 1_000_000,
                "output_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        },
    }
    (proj / "s1.jsonl").write_text(json.dumps(turn) + "\n")
    con = connect(base)
    try:
        # 1e6 input tokens at the Sonnet tier ($3/mtok) = $3.00, not $0.
        cost = con.execute("SELECT round(sum(cost_usd), 6) FROM message_usage").fetchone()[0]
        assert cost == pytest.approx(3.0)
    finally:
        con.close()
