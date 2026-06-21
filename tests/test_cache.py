"""Materialized snapshot (--fast) behavior."""

from __future__ import annotations

from pathlib import Path

import pytest

from ccq import db


def test_build_and_query_snapshot(transcripts_dir: Path, tmp_path: Path) -> None:
    cache = tmp_path / "snap" / "ccq.duckdb"
    out = db.build_cache(transcripts_dir, cache)
    assert out == cache
    assert cache.exists()

    con = db.connect_fast(cache)
    try:
        # Same answers as the live views.
        projects = [r[0] for r in con.execute("SELECT project FROM sessions ORDER BY 1").fetchall()]
        assert projects == ["demo-app", "web-api"]
        assert con.execute("SELECT subagent_tokens FROM agents").fetchone()[0] == 55000
        # events keeps its scalar columns but drops the raw json blob in the snapshot.
        cols = [d[0] for d in con.execute("SELECT * FROM events LIMIT 0").description]
        assert "json" not in cols
        assert "session_id" in cols
    finally:
        con.close()


def test_snapshot_is_read_only(transcripts_dir: Path, tmp_path: Path) -> None:
    cache = tmp_path / "ccq.duckdb"
    db.build_cache(transcripts_dir, cache)
    con = db.connect_fast(cache)
    try:
        with pytest.raises(Exception, match=r"read-only|read only|Cannot execute|Invalid"):
            con.execute("CREATE TABLE t (a INT)")
    finally:
        con.close()


def test_connect_fast_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        db.connect_fast(tmp_path / "nope.duckdb")


def test_default_cache_path_respects_xdg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert db.default_cache_path() == tmp_path / "ccq" / "ccq.duckdb"


def test_default_cache_path_ignores_relative_xdg(monkeypatch: pytest.MonkeyPatch) -> None:
    # A non-absolute XDG_CACHE_HOME is invalid per spec; we fall back to ~/.cache.
    monkeypatch.setenv("XDG_CACHE_HOME", "relative/cache")
    assert db.default_cache_path() == Path.home() / ".cache" / "ccq" / "ccq.duckdb"


def test_is_cache_stale(transcripts_dir: Path, tmp_path: Path) -> None:
    import os  # noqa: PLC0415

    cache = tmp_path / "ccq.duckdb"
    db.build_cache(transcripts_dir, cache)
    # Cache newer than every transcript -> not stale.
    os.utime(cache, (2_000_000_000, 2_000_000_000))
    assert db.is_cache_stale(cache, transcripts_dir) is False
    # Cache older than the transcripts -> stale.
    os.utime(cache, (1_000_000, 1_000_000))
    assert db.is_cache_stale(cache, transcripts_dir) is True


def test_is_cache_stale_missing_is_stale(tmp_path: Path) -> None:
    assert db.is_cache_stale(tmp_path / "nope.duckdb", tmp_path) is True
