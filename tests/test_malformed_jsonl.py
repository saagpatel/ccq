"""Wrong-shape and malformed JSONL must not crash read-only queries.

Synthetic fixtures only: never reads live ``~/.claude/projects``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import duckdb
import pytest
from click.testing import CliRunner

from ccq.cli import cli
from ccq.db import build_cache, connect, connect_fast, is_cache_stale

_PUBLIC_VIEWS = (
    "events",
    "message_usage",
    "tool_calls",
    "errors",
    "agents",
    "agent_results",
    "prompts",
    "sessions",
    "model_pricing",
)

_VALID_USER = {
    "type": "user",
    "sessionId": "s-valid-0000-0000-0000-000000000001",
    "cwd": "/home/user/Projects/demo-app",
    "gitBranch": "main",
    "timestamp": "2026-06-01T10:00:00.000Z",
    "message": {"role": "user", "content": "please refactor the duckdb loader"},
}
_VALID_ASSISTANT = {
    "type": "assistant",
    "sessionId": "s-valid-0000-0000-0000-000000000001",
    "cwd": "/home/user/Projects/demo-app",
    "gitBranch": "main",
    "timestamp": "2026-06-01T10:00:05.000Z",
    "requestId": "req-1",
    "message": {
        "role": "assistant",
        "model": "claude-opus-4-8",
        "usage": {
            "input_tokens": 1000,
            "output_tokens": 2000,
            "cache_creation_input_tokens": 500,
            "cache_read_input_tokens": 10000,
        },
        "content": [
            {
                "type": "tool_use",
                "id": "tu1",
                "name": "Bash",
                "input": {"command": "ls -la /tmp"},
            }
        ],
    },
}


def _write_jsonl(base: Path, name: str, lines: list[object]) -> Path:
    proj = base / "proj"
    proj.mkdir(parents=True, exist_ok=True)
    path = proj / name
    text = [line if isinstance(line, str) else json.dumps(line) for line in lines]
    path.write_text("\n".join(text) + ("\n" if text else ""))
    return path


def _query_all_views(con: duckdb.DuckDBPyConnection) -> None:
    for name in _PUBLIC_VIEWS:
        con.execute(f"SELECT * FROM {name}").fetchall()


def test_wrong_top_level_values_do_not_invent_sessions(tmp_path: Path) -> None:
    path = _write_jsonl(
        tmp_path,
        "wrong.jsonl",
        ["[1,2,3]", '"hello"', "42", "true", "null", "{}"],
    )
    before = path.read_bytes()
    con = connect(tmp_path)
    try:
        _query_all_views(con)
        # Empty object is a JSON object with missing fields: explicit (unknown),
        # not a crash. Non-objects are not entity rows.
        events = con.execute("SELECT session_id, type, project FROM events").fetchall()
        assert events == [(None, None, "(unknown)")]
        assert con.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM message_usage").fetchone()[0] == 0
    finally:
        con.close()
    assert path.read_bytes() == before


@pytest.mark.parametrize("payload", ["[1,2,3]", '"hello"', "42", "true", "null"])
def test_non_object_top_level_is_not_an_event(tmp_path: Path, payload: str) -> None:
    _write_jsonl(tmp_path, "one.jsonl", [payload])
    con = connect(tmp_path)
    try:
        _query_all_views(con)
        assert con.execute("SELECT count(*) FROM events").fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0
    finally:
        con.close()


def test_mixed_valid_and_invalid_preserves_valid_provenance(tmp_path: Path) -> None:
    path = _write_jsonl(
        tmp_path,
        "mixed.jsonl",
        [
            _VALID_USER,
            "{not json",
            [1, 2, 3],
            "null",
            _VALID_ASSISTANT,
            "",
        ],
    )
    con = connect(tmp_path)
    try:
        _query_all_views(con)
        sessions = con.execute("SELECT session_id, project FROM sessions").fetchall()
        assert sessions == [("s-valid-0000-0000-0000-000000000001", "demo-app")]
        tools = con.execute("SELECT tool_name FROM tool_calls").fetchall()
        assert tools == [("Bash",)]
        files = {r[0] for r in con.execute("SELECT filename FROM events").fetchall()}
        assert files == {str(path)}
        cost = con.execute("SELECT round(sum(cost_usd), 6) FROM message_usage").fetchone()[0]
        assert cost == pytest.approx(0.063125)
    finally:
        con.close()


def test_missing_and_wrong_typed_nested_fields_do_not_coerce(tmp_path: Path) -> None:
    _write_jsonl(
        tmp_path,
        "nested.jsonl",
        [
            # Object sessionId must not become a JSON-string session.
            {
                "type": "user",
                "sessionId": {"id": "s1"},
                "cwd": "/home/user/Projects/x",
                "message": {"content": "hi"},
            },
            # Object cwd must not regex into a fake project name.
            {
                "type": "user",
                "sessionId": "s-cwd",
                "cwd": {"path": "/home/user/Projects/secret"},
                "message": {"content": "hi"},
            },
            # Assistant with string message: no model, no invented usage.
            {"type": "assistant", "sessionId": "s-msg", "cwd": "/x", "message": "not-an-object"},
            # Usage present but not an object: do not invent a 0-token priced row.
            {
                "type": "assistant",
                "sessionId": "s-usage",
                "cwd": "/home/user/Projects/x",
                "message": {"role": "assistant", "model": "claude-opus-4-8", "usage": "nope"},
            },
            # Missing usage on an otherwise typed assistant: existing 0-token row.
            {
                "type": "assistant",
                "sessionId": "s-missing",
                "cwd": "/home/user/Projects/x",
                "message": {"model": "claude-opus-4-8"},
            },
            # Content object (not array): no invented tool_use.
            {
                "type": "assistant",
                "sessionId": "s-content",
                "cwd": "/home/user/Projects/x",
                "message": {"model": "m", "content": {"type": "tool_use", "name": "Bash"}},
            },
            # Tool name as array must not stringify into tool_name.
            {
                "type": "assistant",
                "sessionId": "s-toolname",
                "cwd": "/home/user/Projects/x",
                "message": {
                    "model": "m",
                    "content": [{"type": "tool_use", "id": "tu9", "name": ["Bash"], "input": {}}],
                },
            },
        ],
    )
    con = connect(tmp_path)
    try:
        _query_all_views(con)
        session_ids = {r[0] for r in con.execute("SELECT session_id FROM sessions").fetchall()}
        assert '{"id":"s1"}' not in session_ids
        assert "s1" not in session_ids
        cwd_proj = con.execute("SELECT project FROM events WHERE session_id = 's-cwd'").fetchone()
        assert cwd_proj == ("(unknown)",)
        assert (
            con.execute(
                "SELECT count(*) FROM message_usage WHERE session_id = 's-usage'"
            ).fetchone()[0]
            == 0
        )
        missing = con.execute(
            "SELECT input_tokens, output_tokens, cost_usd FROM message_usage "
            "WHERE session_id = 's-missing'"
        ).fetchone()
        assert missing == (0, 0, 0.0)
        names = [r[0] for r in con.execute("SELECT tool_name FROM tool_calls").fetchall()]
        assert '["Bash"]' not in names
        assert "Bash" not in names
    finally:
        con.close()


def test_malformed_lines_do_not_crash(tmp_path: Path) -> None:
    _write_jsonl(
        tmp_path,
        "bad.jsonl",
        ["{not json", '{"a":1,}', '{"a":1}{"b":2}', "\x00\x01"],
    )
    con = connect(tmp_path)
    try:
        _query_all_views(con)
        assert con.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0
        # Salvaged objects (if any) have no string session id; nothing is invented.
        session_ids = [r[0] for r in con.execute("SELECT session_id FROM events").fetchall()]
        assert all(sid is None for sid in session_ids)
    finally:
        con.close()


def test_empty_and_missing_inputs_use_empty_views(tmp_path: Path) -> None:
    empty = tmp_path / "projects"
    empty.mkdir()
    missing = tmp_path / "nope"
    blank = tmp_path / "blank"
    _write_jsonl(blank, "empty.jsonl", [])

    for target in (empty, missing, blank):
        con = connect(target)
        try:
            _query_all_views(con)
            assert con.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0
            assert con.execute("SELECT count(*) FROM events").fetchone()[0] == 0
        finally:
            con.close()


def test_dangling_symlink_is_unavailable_not_a_crash(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "gone.jsonl").symlink_to(tmp_path / "does-not-exist.jsonl")
    con = connect(tmp_path)
    try:
        _query_all_views(con)
        assert con.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0
    finally:
        con.close()


def test_unreadable_file_is_skipped(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root can read chmod-0 files")
    locked = _write_jsonl(tmp_path, "locked.jsonl", [_VALID_USER])
    locked.chmod(0)
    try:
        if os.access(locked, os.R_OK):
            pytest.skip("os.access still reports readable")
        con = connect(tmp_path)
        try:
            _query_all_views(con)
            assert con.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0
        finally:
            con.close()
    finally:
        locked.chmod(0o644)


def test_unreadable_file_does_not_hide_sibling_valid_file(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root can read chmod-0 files")
    locked = _write_jsonl(tmp_path, "locked.jsonl", ["{not json"])
    valid = _write_jsonl(tmp_path, "ok.jsonl", [_VALID_USER, _VALID_ASSISTANT])
    locked.chmod(0)
    try:
        if os.access(locked, os.R_OK):
            pytest.skip("os.access still reports readable")
        con = connect(tmp_path)
        try:
            _query_all_views(con)
            files = {r[0] for r in con.execute("SELECT filename FROM events").fetchall()}
            assert str(valid) in files
            assert str(locked) not in files
            assert con.execute("SELECT count(*) FROM sessions").fetchone()[0] == 1
        finally:
            con.close()
    finally:
        locked.chmod(0o644)


def test_cache_and_staleness_tolerate_malformed_and_dangling(tmp_path: Path) -> None:
    _write_jsonl(tmp_path, "mixed.jsonl", [_VALID_USER, "{not json", _VALID_ASSISTANT, [1, 2, 3]])
    cache = tmp_path / "ccq.duckdb"
    build_cache(tmp_path, cache)
    con = connect_fast(cache)
    try:
        assert con.execute("SELECT project FROM sessions").fetchall() == [("demo-app",)]
    finally:
        con.close()

    (tmp_path / "proj" / "gone.jsonl").symlink_to(tmp_path / "missing.jsonl")
    assert is_cache_stale(cache, tmp_path) is True


def test_unlistable_dir_uses_empty_views(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(self: Path, pattern: str) -> list[Path]:  # noqa: ARG001
        raise OSError

    monkeypatch.setattr("ccq.db.Path.glob", boom)
    con = connect(tmp_path)
    try:
        _query_all_views(con)
        assert con.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0
    finally:
        con.close()


def test_stat_oserror_on_jsonl_is_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_jsonl(tmp_path, "ok.jsonl", [_VALID_USER])
    original = Path.is_file

    def boom(self: Path) -> bool:
        if self.suffix == ".jsonl":
            raise OSError
        return original(self)

    monkeypatch.setattr("ccq.db.Path.is_file", boom)
    con = connect(tmp_path)
    try:
        _query_all_views(con)
        assert con.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0
    finally:
        con.close()


def test_scan_ioexception_falls_back_to_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_jsonl(tmp_path, "ok.jsonl", [_VALID_USER])
    original = duckdb.DuckDBPyConnection.execute

    def execute(self: duckdb.DuckDBPyConnection, *args: object, **kwargs: object) -> object:
        sql = args[0] if args else ""
        if isinstance(sql, str) and "read_ndjson_objects" in sql:
            raise duckdb.IOException
        return original(self, *args, **kwargs)

    monkeypatch.setattr(duckdb.DuckDBPyConnection, "execute", execute)
    con = connect(tmp_path)
    try:
        _query_all_views(con)
        assert con.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0
    finally:
        con.close()


def test_is_cache_stale_unlistable_dir_is_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "ccq.duckdb"
    cache.write_bytes(b"not-a-real-snapshot")

    def boom(self: Path, pattern: str) -> list[Path]:  # noqa: ARG001
        raise OSError

    monkeypatch.setattr("ccq.db.Path.glob", boom)
    assert is_cache_stale(cache, tmp_path) is True


class _GlobRaisesDuringIter:
    """A glob result that fails on ``next``, not at the ``glob()`` call."""

    def __iter__(self) -> _GlobRaisesDuringIter:
        return self

    def __next__(self) -> Path:
        raise OSError


class _GlobRaisesAfterFirst:
    """Yields one path, then raises — the listing-time failure glob actually hits."""

    def __init__(self, first: Path) -> None:
        self._first = first
        self._sent = False

    def __iter__(self) -> _GlobRaisesAfterFirst:
        return self

    def __next__(self) -> Path:
        if not self._sent:
            self._sent = True
            return self._first
        raise OSError


def test_glob_raises_during_iteration_uses_empty_views(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_jsonl(tmp_path, "ok.jsonl", [_VALID_USER])

    def boom(self: Path, pattern: str) -> _GlobRaisesDuringIter:  # noqa: ARG001
        return _GlobRaisesDuringIter()

    monkeypatch.setattr("ccq.db.Path.glob", boom)
    con = connect(tmp_path)
    try:
        _query_all_views(con)
        assert con.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0
    finally:
        con.close()


def test_glob_raises_after_first_yield_uses_empty_views(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write_jsonl(tmp_path, "ok.jsonl", [_VALID_USER])

    def boom(self: Path, pattern: str) -> _GlobRaisesAfterFirst:  # noqa: ARG001
        return _GlobRaisesAfterFirst(path)

    monkeypatch.setattr("ccq.db.Path.glob", boom)
    con = connect(tmp_path)
    try:
        _query_all_views(con)
        assert con.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM events").fetchone()[0] == 0
    finally:
        con.close()


def test_is_cache_stale_glob_raises_during_iteration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "ccq.duckdb"
    cache.write_bytes(b"not-a-real-snapshot")
    os.utime(cache, (2_000_000_000, 2_000_000_000))
    first = _write_jsonl(tmp_path, "ok.jsonl", [_VALID_USER])
    os.utime(first, (1_000_000, 1_000_000))

    def boom(self: Path, pattern: str) -> _GlobRaisesAfterFirst:  # noqa: ARG001
        return _GlobRaisesAfterFirst(first)

    monkeypatch.setattr("ccq.db.Path.glob", boom)
    assert is_cache_stale(cache, tmp_path) is True


_WRONG_ERROR_SHAPES = (
    None,
    [429],
    {"code": 429},
    True,
    429,
)


@pytest.mark.parametrize("status", _WRONG_ERROR_SHAPES)
def test_wrong_shaped_api_error_status_is_unavailable(tmp_path: Path, status: object) -> None:
    path = _write_jsonl(
        tmp_path,
        "err.jsonl",
        [
            {
                "type": "assistant",
                "sessionId": "s-status",
                "cwd": "/home/user/Projects/demo-app",
                "apiErrorStatus": status,
            }
        ],
    )
    con = connect(tmp_path)
    try:
        _query_all_views(con)
        row = con.execute("SELECT api_error_status, is_api_error, filename FROM events").fetchone()
        assert row is not None
        assert row[0] is None
        assert row[1] is None or row[1] is False
        assert row[2] == str(path)
        assert '{"code":429}' not in str(row)
        assert "[429]" not in str(row)
        assert con.execute("SELECT count(*) FROM errors").fetchone()[0] == 0
    finally:
        con.close()


_WRONG_FLAG_SHAPES = (
    None,
    [True],
    {"ok": True},
    "true",
    1,
)


@pytest.mark.parametrize("flag", _WRONG_FLAG_SHAPES)
def test_wrong_shaped_api_error_flag_is_unavailable(tmp_path: Path, flag: object) -> None:
    path = _write_jsonl(
        tmp_path,
        "flag.jsonl",
        [
            {
                "type": "assistant",
                "sessionId": "s-flag",
                "cwd": "/home/user/Projects/demo-app",
                "isApiErrorMessage": flag,
            }
        ],
    )
    con = connect(tmp_path)
    try:
        _query_all_views(con)
        row = con.execute("SELECT is_api_error, api_error_status, filename FROM events").fetchone()
        assert row is not None
        assert row[0] is not True
        assert row[1] is None
        assert row[2] == str(path)
        assert con.execute("SELECT count(*) FROM errors").fetchone()[0] == 0
    finally:
        con.close()


def test_valid_api_error_shapes_and_provenance_are_preserved(tmp_path: Path) -> None:
    path = _write_jsonl(
        tmp_path,
        "ok-err.jsonl",
        [
            {
                "type": "assistant",
                "sessionId": "s-ok",
                "cwd": "/home/user/Projects/demo-app",
                "isApiErrorMessage": True,
                "apiErrorStatus": "429",
                "message": {"role": "assistant", "model": "claude-opus-4-8"},
            },
            {
                "type": "assistant",
                "sessionId": "s-wrong",
                "cwd": "/home/user/Projects/demo-app",
                "isApiErrorMessage": {"flag": True},
                "apiErrorStatus": 429,
            },
        ],
    )
    con = connect(tmp_path)
    try:
        _query_all_views(con)
        errors = con.execute("SELECT session_id, status FROM errors ORDER BY session_id").fetchall()
        assert errors == [("s-ok", "429")]
        files = {r[0] for r in con.execute("SELECT filename FROM events").fetchall()}
        assert files == {str(path)}
        flag_wrong = con.execute(
            "SELECT is_api_error, api_error_status FROM events WHERE session_id = 's-wrong'"
        ).fetchone()
        assert flag_wrong == (None, None)
    finally:
        con.close()


def test_cli_empty_and_unknown_session_states(tmp_path: Path) -> None:
    empty = tmp_path / "projects"
    empty.mkdir()
    runner = CliRunner()
    sessions = runner.invoke(cli, ["--projects-dir", str(empty), "sessions"])
    assert sessions.exit_code == 0
    assert "(no rows)" in sessions.output

    missing = runner.invoke(cli, ["--projects-dir", str(empty), "session", "zzzzzzzz"])
    assert missing.exit_code == 1
    assert "No session matching prefix" in missing.output

    _write_jsonl(tmp_path, "mixed.jsonl", ["[1,2,3]", "{not json", _VALID_USER])
    search = runner.invoke(cli, ["--projects-dir", str(tmp_path), "search", "duckdb"])
    assert search.exit_code == 0
    assert "demo-app" in search.output
