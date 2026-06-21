"""CLI smoke tests via Click's runner against the synthetic corpus."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from ccq.cli import cli

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def run(transcripts_dir: Path) -> object:
    runner = CliRunner()

    def _invoke(*args: str) -> object:
        return runner.invoke(cli, ["--projects-dir", str(transcripts_dir), *args])

    return _invoke


def test_cost_by_model(run: object) -> None:
    result = run("cost", "--by", "model")
    assert result.exit_code == 0
    assert "claude-opus-4-8" in result.output
    assert "claude-sonnet-4-6" in result.output


def test_sessions_lists_projects(run: object) -> None:
    result = run("sessions")
    assert result.exit_code == 0
    assert "demo-app" in result.output
    assert "web-api" in result.output


def test_tools_frequency(run: object) -> None:
    result = run("tools")
    assert result.exit_code == 0
    assert "Bash" in result.output
    assert "Read" in result.output


def test_agents_by_type(run: object) -> None:
    result = run("agents", "--by", "type")
    assert result.exit_code == 0
    assert "Explore" in result.output
    assert "55,000" in result.output


def test_errors_rollup(run: object) -> None:
    result = run("errors")
    assert result.exit_code == 0
    assert "429" in result.output


def test_search_finds_session(run: object) -> None:
    result = run("search", "duckdb")
    assert result.exit_code == 0
    assert "demo-app" in result.output


def test_session_timeline(run: object) -> None:
    result = run("session", "aaaa1111")
    assert result.exit_code == 0
    assert "tool:Bash" in result.output
    assert "ERROR 429" in result.output


def test_session_unknown_prefix_exits_nonzero(run: object) -> None:
    result = run("session", "zzzzzzzz")
    assert result.exit_code == 1


def test_sql_escape_hatch(run: object) -> None:
    result = run("sql", "SELECT count(*) AS n FROM sessions")
    assert result.exit_code == 0
    assert "2" in result.output


def test_sql_guard_refuses_write(run: object) -> None:
    result = run("sql", "DROP VIEW sessions")
    assert result.exit_code == 2
    assert "Refused" in result.output


def test_json_format(run: object) -> None:
    result = run("cost", "--by", "model", "-f", "json")
    assert result.exit_code == 0
    assert '"model"' in result.output
