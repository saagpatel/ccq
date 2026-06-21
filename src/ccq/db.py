"""Read-only DuckDB layer over Claude Code's JSONL transcripts.

The transcripts live at ``~/.claude/projects/<encoded-path>/<session-uuid>.jsonl``.
We never copy or mutate them: ``read_ndjson_objects`` scans each line as an opaque
``JSON`` value (zero schema inference - the records are heterogeneous), and a small
set of SQL views extract the entities we care about.

Two real-data lessons are encoded here:
- Heterogeneous lines break a hard ``CAST``; everything numeric uses ``TRY_CAST``.
- A token cast over the unfiltered scan gets reordered ahead of the type filter by
  the optimizer and blows up, so every view that casts tokens reads from a
  type-filtered subquery barrier.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

import duckdb

from ccq.pricing import CACHE_READ_MULT, CACHE_WRITE_MULT, pricing_rows

if TYPE_CHECKING:
    from collections.abc import Iterable

DEFAULT_PROJECTS_DIR = Path.home() / ".claude" / "projects"
# SQL-literal of the user's home dir, used to label a session run from $HOME itself.
_HOME_LITERAL = str(Path.home()).replace("'", "''")


# Map a working-directory path to a human project name. Anchors mirror
# reference_session_project_mapping; falls back to the final path segment.
# Parameterized by the SQL expression that yields the cwd, because DuckDB does
# not allow referencing a sibling column alias within the same SELECT.
def _project_expr(cwd_sql: str) -> str:
    c = cwd_sql
    return (
        "COALESCE("
        rf"NULLIF(regexp_extract({c}, '/Projects/([^/]+)', 1), ''),"
        rf"NULLIF(regexp_extract({c}, '/\.local/share/([^/]+)', 1), ''),"
        rf"NULLIF(regexp_extract({c}, '/Documents/([^/]+)', 1), ''),"
        rf"CASE WHEN {c} = '{_HOME_LITERAL}' THEN '(home)' END,"
        rf"NULLIF(regexp_extract({c}, '([^/]+)$', 1), ''),"
        "'(unknown)')"
    )


_CWD = "json_extract_string(json, '$.cwd')"

# Statements we refuse to run via the `sql` escape hatch. This is a personal tool
# over the operator's own data, so the goal is "don't accidentally write to or
# attach disk", not an untrusted-SQL fortress. Single read-only statement only.
# `pragma` is intentionally NOT an allowed leader (some PRAGMAs mutate session
# state); the read-only `pragma_*` table functions remain usable inside a SELECT.
_SQL_ALLOWED_LEADERS = ("select", "with", "explain", "describe", "summarize", "show")
# Matched as whole words (\bWORD\b) so `load('x.so')` is caught as readily as
# `LOAD 'x.so'`, while identifiers like `payload`, `created_at`, `pragma_table_info`
# do not false-trip (the underscore/letter neighbours suppress the word boundary).
_SQL_DENY_KEYWORDS = (
    "attach",
    "detach",
    "copy",
    "install",
    "load",
    "export",
    "import",
    "create",
    "insert",
    "update",
    "delete",
    "drop",
    "alter",
    "pragma",
)
_SQL_DENY_RE = re.compile(r"\b(" + "|".join(_SQL_DENY_KEYWORDS) + r")\b")


class UnsafeSQLError(ValueError):
    """Raised when a user-supplied SQL statement is not a single read-only query."""


def _glob(projects_dir: Path) -> str:
    return str(Path(projects_dir) / "*" / "*.jsonl")


def _views_sql() -> str:
    cost_expr = (
        "(u.input_tokens * COALESCE(p.in_price, 0)"
        f" + u.cache_creation_tokens * COALESCE(p.in_price, 0) * {CACHE_WRITE_MULT}"
        f" + u.cache_read_tokens * COALESCE(p.in_price, 0) * {CACHE_READ_MULT}"
        " + u.output_tokens * COALESCE(p.out_price, 0)) / 1e6"
    )
    return rf"""
-- Every line, common scalar fields. No numeric casts here (keep the base scan safe).
CREATE VIEW events AS
SELECT
    filename,
    json_extract_string(json, '$.sessionId')        AS session_id,
    json_extract_string(json, '$.type')             AS type,
    json_extract_string(json, '$.uuid')             AS uuid,
    json_extract_string(json, '$.parentUuid')       AS parent_uuid,
    TRY_CAST(json_extract_string(json, '$.timestamp') AS TIMESTAMP) AS ts,
    json_extract_string(json, '$.cwd')              AS cwd,
    {_project_expr(_CWD)}                         AS project,
    json_extract_string(json, '$.gitBranch')        AS git_branch,
    json_extract_string(json, '$.requestId')        AS request_id,
    json_extract_string(json, '$.version')          AS version,
    json_extract_string(json, '$.message.role')     AS role,
    json_extract_string(json, '$.message.model')    AS model,
    json_extract_string(json, '$.apiErrorStatus')   AS api_error_status,
    json_extract_string(json, '$.isApiErrorMessage') = 'true' AS is_api_error,
    json
FROM raw;

-- Per assistant turn: token usage + estimated USD (main-loop only).
CREATE VIEW message_usage AS
SELECT
    u.session_id, u.project, u.cwd, u.git_branch, u.ts, u.model, u.request_id,
    u.input_tokens, u.output_tokens, u.cache_creation_tokens, u.cache_read_tokens,
    {cost_expr} AS cost_usd
FROM (
    SELECT
        json_extract_string(json, '$.sessionId') AS session_id,
        {_project_expr(_CWD)} AS project,
        json_extract_string(json, '$.cwd') AS cwd,
        json_extract_string(json, '$.gitBranch') AS git_branch,
        TRY_CAST(json_extract_string(json, '$.timestamp') AS TIMESTAMP) AS ts,
        json_extract_string(json, '$.message.model') AS model,
        json_extract_string(json, '$.requestId') AS request_id,
        COALESCE(TRY_CAST(json_extract_string(json, '$.message.usage.input_tokens') AS BIGINT), 0) AS input_tokens,
        COALESCE(TRY_CAST(json_extract_string(json, '$.message.usage.output_tokens') AS BIGINT), 0) AS output_tokens,
        COALESCE(TRY_CAST(json_extract_string(json, '$.message.usage.cache_creation_input_tokens') AS BIGINT), 0) AS cache_creation_tokens,
        COALESCE(TRY_CAST(json_extract_string(json, '$.message.usage.cache_read_input_tokens') AS BIGINT), 0) AS cache_read_tokens
    FROM raw
    WHERE json_extract_string(json, '$.type') = 'assistant'
      AND json_extract_string(json, '$.message.model') IS NOT NULL
) u
LEFT JOIN model_pricing p ON p.model = u.model;

-- One row per tool invocation (unnested from assistant message.content).
CREATE VIEW tool_calls AS
SELECT
    a.session_id, a.project, a.cwd, a.ts,
    tc->>'$.name'  AS tool_name,
    tc->>'$.id'    AS tool_use_id,
    tc->'$.input'  AS tool_input
FROM (
    SELECT
        json_extract_string(json, '$.sessionId') AS session_id,
        {_project_expr(_CWD)} AS project,
        json_extract_string(json, '$.cwd') AS cwd,
        TRY_CAST(json_extract_string(json, '$.timestamp') AS TIMESTAMP) AS ts,
        json->'$.message.content' AS content
    FROM raw
    WHERE json_extract_string(json, '$.type') = 'assistant'
) a,
UNNEST(
    CASE WHEN json_type(a.content) = 'ARRAY'
         THEN TRY_CAST(a.content AS JSON[])
         ELSE CAST([] AS JSON[]) END
) AS t(tc)
WHERE tc->>'$.type' = 'tool_use';

-- API errors / retries (rate limits, transient failures).
CREATE VIEW errors AS
SELECT session_id, project, cwd, ts, model, request_id,
       COALESCE(api_error_status, 'flagged') AS status
FROM events
WHERE is_api_error OR api_error_status IS NOT NULL;

-- Subagent dispatches joined to the token total reported on their result.
CREATE VIEW agent_results AS
SELECT
    session_id,
    r->>'$.tool_use_id' AS tool_use_id,
    TRY_CAST(json_extract_string(json, '$.toolUseResult.totalTokens') AS BIGINT) AS total_tokens
FROM (
    SELECT json, json_extract_string(json, '$.sessionId') AS session_id FROM raw
    WHERE json_extract_string(json, '$.type') = 'user'
      AND json_extract_string(json, '$.toolUseResult.totalTokens') IS NOT NULL
) ,
UNNEST(
    CASE WHEN json_type(json->'$.message.content') = 'ARRAY'
         THEN TRY_CAST(json->'$.message.content' AS JSON[])
         ELSE CAST([] AS JSON[]) END
) AS t(r)
WHERE r->>'$.type' = 'tool_result';

CREATE VIEW agents AS
SELECT
    tc.session_id, tc.project, tc.ts,
    tc.tool_input->>'$.subagent_type' AS subagent_type,
    tc.tool_input->>'$.model'         AS model,
    tc.tool_input->>'$.description'    AS description,
    ar.total_tokens                   AS subagent_tokens
FROM tool_calls tc
LEFT JOIN agent_results ar
       ON ar.tool_use_id = tc.tool_use_id AND ar.session_id = tc.session_id
WHERE tc.tool_name = 'Agent';

-- One row per session: span, volume, models, tokens, estimated cost.
CREATE VIEW sessions AS
WITH msg AS (
    SELECT session_id,
           COALESCE(mode(project) FILTER (WHERE project <> '(unknown)'), '(unknown)') AS project,
           min(ts) AS started_at, max(ts) AS ended_at,
           count(*) FILTER (WHERE type IN ('user', 'assistant')) AS messages,
           count(*) FILTER (WHERE type = 'user') AS user_turns,
           count(*) FILTER (WHERE type = 'assistant') AS assistant_turns,
           list_distinct(list(model) FILTER (WHERE model IS NOT NULL)) AS models,
           any_value(git_branch) AS git_branch
    FROM events
    WHERE session_id IS NOT NULL
    GROUP BY session_id
),
cost AS (
    SELECT session_id,
           -- composite: raw input + cache-write + cache-read tokens
           sum(input_tokens + cache_creation_tokens + cache_read_tokens) AS total_input_tokens,
           sum(output_tokens) AS output_tokens,
           sum(cost_usd) AS cost_usd
    FROM message_usage GROUP BY session_id
)
SELECT m.session_id, m.project, m.git_branch,
       m.started_at, m.ended_at,
       date_diff('minute', m.started_at, m.ended_at) AS duration_min,
       m.messages, m.user_turns, m.assistant_turns, m.models,
       COALESCE(c.total_input_tokens, 0) AS total_input_tokens,
       COALESCE(c.output_tokens, 0) AS output_tokens,
       COALESCE(c.cost_usd, 0.0) AS cost_usd
FROM msg m LEFT JOIN cost c ON c.session_id = m.session_id;

-- Searchable text: typed user prompts (string content only) + session titles.
CREATE VIEW prompts AS
SELECT * FROM (
    SELECT
        json_extract_string(json, '$.sessionId') AS session_id,
        {_project_expr(_CWD)} AS project,
        TRY_CAST(json_extract_string(json, '$.timestamp') AS TIMESTAMP) AS ts,
        'prompt' AS kind,
        json_extract_string(json, '$.message.content') AS text
    FROM raw
    WHERE json_extract_string(json, '$.type') = 'user'
      AND json_type(json->'$.message.content') = 'VARCHAR'
    UNION ALL
    SELECT
        json_extract_string(json, '$.sessionId'),
        NULL, NULL, 'title',
        json_extract_string(json, '$.aiTitle')
    FROM raw
    WHERE json_extract_string(json, '$.type') = 'ai-title'
)
WHERE text IS NOT NULL AND length(trim(text)) > 0;
"""


# Derived relations, in dependency order. Materialized into tables by build_cache.
_MATERIALIZED = (
    "events",
    "message_usage",
    "tool_calls",
    "errors",
    "agent_results",
    "agents",
    "prompts",
    "sessions",
)


def _install_relations(con: duckdb.DuckDBPyConnection, projects_dir: Path | str) -> None:
    """Create the `raw` view, the pricing table, and all derived views on *con*."""
    # read_ndjson_objects raises on a zero-match glob, so fall back to an empty
    # typed relation when there are no transcripts yet (fresh machine / empty dir).
    matches = list(Path(projects_dir).glob("*/*.jsonl"))
    if matches:
        # The glob is operator-controlled config (not user input); inline it with
        # quote-escaping since DuckDB cannot bind a prepared parameter inside DDL.
        glob_literal = _glob(Path(projects_dir)).replace("'", "''")
        con.execute(
            "CREATE VIEW raw AS SELECT filename, json FROM "
            f"read_ndjson_objects('{glob_literal}', filename=true, ignore_errors=true)"
        )
    else:
        con.execute(
            "CREATE VIEW raw AS SELECT * FROM "
            "(SELECT NULL::VARCHAR AS filename, NULL::JSON AS json) WHERE false"
        )
    con.execute("CREATE TABLE model_pricing (model VARCHAR, in_price DOUBLE, out_price DOUBLE)")
    con.executemany("INSERT INTO model_pricing VALUES (?, ?, ?)", pricing_rows())
    con.execute(_views_sql())


def connect(projects_dir: Path | str = DEFAULT_PROJECTS_DIR) -> duckdb.DuckDBPyConnection:
    """Open an in-memory DuckDB connection with views over the transcripts.

    The source JSONL is only ever scanned, never written. Raises no error if the
    directory is empty - views simply return no rows.
    """
    con = duckdb.connect(":memory:")
    _install_relations(con, projects_dir)
    return con


def default_cache_path() -> Path:
    """XDG cache location for the materialized DuckDB snapshot (never under ~/.claude).

    Per the XDG spec, a non-absolute ``XDG_CACHE_HOME`` is invalid and ignored.
    """
    base = os.environ.get("XDG_CACHE_HOME")
    root = Path(base) if base and Path(base).is_absolute() else Path.home() / ".cache"
    return root / "ccq" / "ccq.duckdb"


def build_cache(
    projects_dir: Path | str = DEFAULT_PROJECTS_DIR, cache_path: Path | None = None
) -> Path:
    """Materialize the derived relations into a persistent DuckDB file for fast reads.

    Rebuilds from scratch each call. The transcripts are only read; the snapshot is
    written to the cache path, not into ~/.claude.
    """
    cache_path = cache_path or default_cache_path()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    # Build into a sibling temp file and swap it in atomically, so a crash mid-build
    # leaves the previous good snapshot in place rather than a half-written one that
    # connect_fast would later open and choke on.
    tmp_path = cache_path.with_name(cache_path.name + ".tmp")
    tmp_path.unlink(missing_ok=True)
    con = duckdb.connect(str(tmp_path))
    try:
        _install_relations(con, projects_dir)
        for name in _MATERIALIZED:
            # The events.json passthrough is the bulk of the data and is only useful
            # for live ad-hoc queries; drop it from the snapshot to keep it lean.
            select = "* EXCLUDE (json)" if name == "events" else "*"
            con.execute(f"CREATE TABLE _m_{name} AS SELECT {select} FROM {name}")
        # Drop derived views in reverse dependency order, then the base view.
        for name in reversed(_MATERIALIZED):
            con.execute(f"DROP VIEW {name}")
        con.execute("DROP VIEW raw")
        for name in _MATERIALIZED:
            con.execute(f"ALTER TABLE _m_{name} RENAME TO {name}")
    finally:
        con.close()
    tmp_path.replace(cache_path)  # atomic on POSIX: readers see old or new, never partial
    return cache_path


def connect_fast(cache_path: Path | None = None) -> duckdb.DuckDBPyConnection:
    """Open the materialized snapshot read-only. Raises FileNotFoundError if absent.

    EAFP (no exists-then-open race): a read-only open of a missing file raises
    ``duckdb.IOException``; we normalize that to ``FileNotFoundError`` (which the CLI
    uses to auto-build on first --fast use) and re-raise any other open failure.
    """
    path = Path(cache_path or default_cache_path())
    try:
        return duckdb.connect(str(path), read_only=True)
    except duckdb.Error as exc:
        if not path.exists():
            raise FileNotFoundError(path) from exc
        raise


def is_cache_stale(
    cache_path: Path | None = None, projects_dir: Path | str = DEFAULT_PROJECTS_DIR
) -> bool:
    """True if any transcript is newer than the snapshot (or the snapshot is missing).

    The snapshot mtime is read with a single ``stat`` (no exists-then-stat race): a
    snapshot deleted concurrently is treated as stale rather than crashing the CLI.
    """
    path = Path(cache_path or default_cache_path())
    try:
        built = path.stat().st_mtime
    except FileNotFoundError:
        return True
    return any(p.stat().st_mtime > built for p in Path(projects_dir).glob("*/*.jsonl"))


def _scan_string_literal(sql: str, i: int) -> int:
    """Return the index just past a single-quoted literal opening at *i*.

    ``''`` escapes a quote (DuckDB rule). Raises on an unterminated literal so the
    guard fails closed rather than guessing where the string ends.
    """
    i += 1  # past the opening quote
    while i < len(sql):
        if sql[i] == "'":
            if sql[i + 1 : i + 2] == "'":  # '' -> escaped quote, stay in the literal
                i += 2
                continue
            return i + 1
        i += 1
    msg = "unterminated string literal"
    raise UnsafeSQLError(msg)


def _scan_block_comment(sql: str, i: int) -> int:
    """Return the index just past a ``/* */`` comment opening at *i*; raise if unterminated."""
    i += 2  # past the opening /*
    while i < len(sql):
        if sql[i : i + 2] == "*/":
            return i + 2
        i += 1
    msg = "unterminated block comment"
    raise UnsafeSQLError(msg)


def _sql_skeleton(sql: str) -> str:
    """Return *sql* with string literals and comments blanked, for structural checks.

    A single pass so the two contexts cannot confuse each other: a ``--`` inside a
    string literal is not a comment, and a ``'`` inside a comment is not a quote.
    Single-quoted literals, ``--`` line comments and ``/* */`` block comments are each
    replaced by a space; whitespace is collapsed. Without this, searching prompt text
    for an ordinary verb (``... LIKE '%delete%'``) or a flag (``'%--frozen%'``) would
    false-trip the deny-list or comment stripper.

    The literal/comment scanners fail closed on an unterminated construct, so the
    skeleton can never disagree with DuckDB's own lexer about where a string ends -
    the only way a hidden statement could otherwise survive skeletonization.
    """
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        c, nxt = sql[i], sql[i + 1] if i + 1 < n else ""
        if c == "'":  # string literal
            i = _scan_string_literal(sql, i)
            out.append(" ")
        elif c == "-" and nxt == "-":  # line comment to end of line
            while i < n and sql[i] != "\n":
                i += 1
            out.append(" ")
        elif c == "/" and nxt == "*":  # block comment (may span lines)
            i = _scan_block_comment(sql, i)
            out.append(" ")
        else:
            out.append(c)
            i += 1
    return " ".join("".join(out).split())


def assert_read_only(sql: str) -> None:
    """Validate that *sql* is a single read-only statement, else raise UnsafeSQLError.

    Structural checks run on the literal/comment-free skeleton, so denied keywords
    and statement separators are only ever matched in real SQL, never inside a
    string the operator is searching for.
    """
    skeleton = _sql_skeleton(sql)
    if not skeleton:
        msg = "empty SQL statement"
        raise UnsafeSQLError(msg)
    # One trailing ";" is a common habit; anything beyond it is a second statement.
    if ";" in skeleton.removesuffix(";"):
        msg = "only a single statement is allowed"
        raise UnsafeSQLError(msg)
    lowered = skeleton.lower()
    if not lowered.startswith(_SQL_ALLOWED_LEADERS):
        msg = f"statement must start with one of {_SQL_ALLOWED_LEADERS}"
        raise UnsafeSQLError(msg)
    bad = _SQL_DENY_RE.search(lowered)
    if bad:
        msg = f"disallowed keyword in read-only query: {bad.group(1)!r}"
        raise UnsafeSQLError(msg)


def run_read_only(
    con: duckdb.DuckDBPyConnection, sql: str, limit: int | None = None
) -> tuple[list[str], list[tuple[object, ...]]]:
    """Run a validated read-only query, returning (column_names, rows).

    When *limit* is set, at most ``limit + 1`` rows are fetched (the extra row lets
    the caller detect truncation) instead of the whole result. The web viewer uses
    this to bound memory on an unrestricted ``SELECT`` typed into its SQL box.
    """
    assert_read_only(sql)
    cur = con.execute(sql)
    columns = [d[0] for d in cur.description] if cur.description else []
    rows = cur.fetchall() if limit is None else cur.fetchmany(limit + 1)
    return columns, rows


def query(
    con: duckdb.DuckDBPyConnection, sql: str, params: Iterable[object] | None = None
) -> tuple[list[str], list[tuple[object, ...]]]:
    """Run a trusted internal query (parameterized), returning (column_names, rows)."""
    cur = con.execute(sql, list(params) if params is not None else None)
    columns = [d[0] for d in cur.description] if cur.description else []
    return columns, cur.fetchall()
