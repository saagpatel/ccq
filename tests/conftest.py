"""Synthetic transcript fixtures. Never touches the real ~/.claude/projects."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from ccq.db import connect

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    import duckdb

# Session A: project "demo-app", one priced assistant turn, a Bash + Agent dispatch,
# an Agent result carrying totalTokens, a 429 error, and a title.
_SID_A = "aaaa1111-0000-0000-0000-000000000001"
_CWD_A = "/home/user/Projects/demo-app"
_SESSION_A = [
    {
        "type": "user",
        "sessionId": _SID_A,
        "cwd": _CWD_A,
        "gitBranch": "main",
        "timestamp": "2026-06-01T10:00:00.000Z",
        "message": {"role": "user", "content": "please refactor the duckdb loader"},
    },
    {
        "type": "assistant",
        "sessionId": _SID_A,
        "cwd": _CWD_A,
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
                },
                {
                    "type": "tool_use",
                    "id": "tu2",
                    "name": "Agent",
                    "input": {
                        "subagent_type": "Explore",
                        "model": "haiku",
                        "description": "find configs",
                    },
                },
            ],
        },
    },
    {
        "type": "user",
        "sessionId": _SID_A,
        "cwd": _CWD_A,
        "timestamp": "2026-06-01T10:00:30.000Z",
        "toolUseResult": {"totalTokens": 55000},
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "tu2", "content": "done"}],
        },
    },
    {
        "type": "assistant",
        "sessionId": _SID_A,
        "cwd": _CWD_A,
        "timestamp": "2026-06-01T10:01:00.000Z",
        "isApiErrorMessage": True,
        "apiErrorStatus": "429",
        "message": {"role": "assistant", "content": [{"type": "text", "text": "rate limited"}]},
    },
    {"type": "ai-title", "sessionId": _SID_A, "aiTitle": "Refactor DuckDB loader"},
]

# Session B: project "web-api" (resolved from a .local/share cwd), Sonnet turn.
_SID_B = "bbbb2222-0000-0000-0000-000000000002"
_CWD_B = "/home/user/.local/share/web-api/app"
_SESSION_B = [
    {
        "type": "user",
        "sessionId": _SID_B,
        "cwd": _CWD_B,
        "gitBranch": "feat/x",
        "timestamp": "2026-06-02T09:00:00.000Z",
        "message": {"role": "user", "content": "run the migration"},
    },
    {
        "type": "assistant",
        "sessionId": _SID_B,
        "cwd": _CWD_B,
        "gitBranch": "feat/x",
        "timestamp": "2026-06-02T09:00:10.000Z",
        "message": {
            "role": "assistant",
            "model": "claude-sonnet-4-6",
            "usage": {
                "input_tokens": 2000,
                "output_tokens": 1000,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
            "content": [
                {"type": "tool_use", "id": "tu3", "name": "Read", "input": {"file_path": "/x"}}
            ],
        },
    },
]


def _write(base: Path, encoded: str, session_id: str, lines: list[dict]) -> None:
    proj = base / encoded
    proj.mkdir(parents=True, exist_ok=True)
    (proj / f"{session_id}.jsonl").write_text("\n".join(json.dumps(line) for line in lines) + "\n")


@pytest.fixture
def transcripts_dir(tmp_path: Path) -> Path:
    """A synthetic projects directory with two sessions across two projects."""
    base = tmp_path / "projects"
    _write(base, "-home-user-Projects-demo-app", _SID_A, _SESSION_A)
    _write(base, "-home-user--local-share-web-api-app", _SID_B, _SESSION_B)
    return base


@pytest.fixture
def con(transcripts_dir: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    connection = connect(transcripts_dir)
    yield connection
    connection.close()


@pytest.fixture
def session_ids() -> dict[str, str]:
    return {"a": _SID_A, "b": _SID_B}
