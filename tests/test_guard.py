"""Read-only SQL guard."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from ccq.db import UnsafeSQLError, assert_read_only, run_read_only

if TYPE_CHECKING:
    import duckdb


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "  select * from sessions",
        "WITH x AS (SELECT 1 AS a) SELECT a FROM x",
        "-- a comment\nSELECT 1",
        "EXPLAIN SELECT 1",
        "SELECT 1;",  # single trailing semicolon is fine
    ],
)
def test_accepts_read_only(sql: str) -> None:
    assert_read_only(sql)  # must not raise


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE model_pricing",
        "DELETE FROM sessions",
        "INSERT INTO model_pricing VALUES ('x', 1, 1)",
        "UPDATE model_pricing SET in_price = 0",
        "ATTACH 'evil.db' AS e",
        "COPY sessions TO 'out.csv'",
        "INSTALL sqlite",
        "CREATE TABLE t (a INT)",
        "SELECT 1; DROP TABLE model_pricing",
        # F-06: function-form extension load must NOT slip past as a SELECT.
        "SELECT load('/tmp/evil.so')",
        "SELECT install('sqlite')",
        "COPY(SELECT 1)TO '/tmp/x'",
        "PRAGMA database_list",  # pragma is no longer an allowed leader
        "",
        "   ",
    ],
)
def test_rejects_writes(sql: str) -> None:
    with pytest.raises(UnsafeSQLError):
        assert_read_only(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM pragma_table_info('sessions')",  # read-only table fn, not PRAGMA
        "SELECT payload, created_at FROM events",  # identifiers must not false-trip
        "SELECT 1 /* a stray update note */",  # denied word only inside a block comment
    ],
)
def test_word_boundary_no_false_positive(sql: str) -> None:
    assert_read_only(sql)  # must not raise


@pytest.mark.parametrize(
    "sql",
    [
        # Searching prompt text for common English verbs must not trip the deny-list.
        "SELECT count(*) FROM prompts WHERE text ILIKE '%delete%'",
        "SELECT text FROM prompts WHERE text LIKE '%create a skill%'",
        "SELECT 1 WHERE 'drop the database' = 'drop the database'",
        "SELECT text FROM prompts WHERE text ILIKE '%don''t update%'",  # escaped quote
        "SELECT 'a;b' AS s",  # a semicolon inside a literal is not a statement break
    ],
)
def test_keyword_inside_string_literal_ok(sql: str) -> None:
    assert_read_only(sql)  # must not raise


@pytest.mark.parametrize(
    "sql",
    [
        # Stripping literals must NOT let a real second statement slip through.
        "SELECT 'x'; DROP TABLE model_pricing",
        "SELECT 'safe' AS s; DELETE FROM sessions",
    ],
)
def test_literal_stripping_still_blocks_trailing_write(sql: str) -> None:
    with pytest.raises(UnsafeSQLError):
        assert_read_only(sql)


@pytest.mark.parametrize(
    "sql",
    [
        # Fail closed on malformed input so the skeleton never disagrees with the
        # DuckDB lexer about where a string ends. The first is the '' -escape edge a
        # reviewer flagged: skeletonizing it leaves a dangling open quote.
        "SELECT 'a''; ATTACH 'evil.db'",
        "SELECT 'unterminated",
        "SELECT 1 /* unterminated comment",
        "SELECT 1;;",  # only a single trailing ';' is tolerated
    ],
)
def test_rejects_malformed_and_extra_statements(sql: str) -> None:
    with pytest.raises(UnsafeSQLError):
        assert_read_only(sql)


def test_run_read_only_returns_rows(con: duckdb.DuckDBPyConnection) -> None:
    cols, rows = run_read_only(con, "SELECT project FROM sessions ORDER BY project")
    assert cols == ["project"]
    assert [r[0] for r in rows] == ["demo-app", "web-api"]


def test_run_read_only_blocks_write(con: duckdb.DuckDBPyConnection) -> None:
    with pytest.raises(UnsafeSQLError):
        run_read_only(con, "DROP VIEW sessions")
