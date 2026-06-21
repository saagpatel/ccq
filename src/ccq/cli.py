"""ccq - query your own Claude Code agent history.

Read-only DuckDB over ~/.claude/projects/*.jsonl. Never writes the transcripts.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import click

from ccq import db
from ccq.format import render

if TYPE_CHECKING:
    from collections.abc import Sequence

    import duckdb

FORMATS = ["table", "json", "csv"]
_fmt_option = click.option(
    "-f",
    "--format",
    "fmt",
    type=click.Choice(FORMATS),
    default="table",
    help="Output format.",
)


def _emit(columns: list[str], rows: Sequence[tuple[object, ...]], fmt: str) -> None:
    click.echo(render(columns, rows, fmt))


def _con(ctx: click.Context) -> duckdb.DuckDBPyConnection:
    """Connect live, or to the materialized snapshot when --fast is set.

    In fast mode the snapshot is built on first use if it does not exist yet.
    """
    if ctx.obj.get("fast"):
        projects_dir = ctx.obj["projects_dir"]
        try:
            con = db.connect_fast()
        except FileNotFoundError:
            click.echo("Building snapshot (first --fast run)...", err=True)
            db.build_cache(projects_dir)
            return db.connect_fast()
        # Use the existing snapshot but flag staleness (rebuilding on every query
        # would thrash during an active session as transcripts grow).
        if db.is_cache_stale(projects_dir=projects_dir):
            click.echo("Note: snapshot is stale; run `ccq cache build` to refresh.", err=True)
        return con
    return db.connect(ctx.obj["projects_dir"])


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "--projects-dir",
    default=str(db.DEFAULT_PROJECTS_DIR),
    help="Directory of Claude Code project transcripts.",
    show_default=True,
)
@click.option(
    "--fast",
    "-F",
    is_flag=True,
    help="Query the materialized snapshot (instant repeat queries; build with `ccq cache build`).",
)
@click.version_option(package_name="ccq")
@click.pass_context
def cli(ctx: click.Context, projects_dir: str, fast: bool) -> None:
    """Query your own Claude Code agent history."""
    ctx.ensure_object(dict)
    ctx.obj["projects_dir"] = projects_dir
    ctx.obj["fast"] = fast


# --------------------------------------------------------------------------- #
# sessions
# --------------------------------------------------------------------------- #
_SESSION_SORTS = {
    "recent": "started_at",
    "cost": "cost_usd",
    "duration": "duration_min",
    "messages": "messages",
}


@cli.command()
@click.option("--project", "-p", help="Filter to projects matching this (case-insensitive).")
@click.option("--since", help="Only sessions started on/after this date (YYYY-MM-DD).")
@click.option("--sort", type=click.Choice(list(_SESSION_SORTS)), default="recent", help="Sort key.")
@click.option("--limit", "-n", default=25, help="Max rows.")
@_fmt_option
@click.pass_context
def sessions(
    ctx: click.Context, project: str | None, since: str | None, sort: str, limit: int, fmt: str
) -> None:
    """List sessions with project, span, volume, tokens and estimated cost."""
    order = _SESSION_SORTS[sort]
    sql = f"""
        SELECT left(session_id, 8) AS id, project, started_at, duration_min AS dur_min,
               messages AS msgs, models, output_tokens AS out_tok,
               round(cost_usd, 2) AS cost_usd
        FROM sessions
        WHERE (? IS NULL OR project ILIKE '%' || ? || '%')
          AND (? IS NULL OR started_at >= TRY_CAST(? AS TIMESTAMP))
        ORDER BY {order} DESC NULLS LAST
        LIMIT ?
    """
    cols, rows = db.query(_con(ctx), sql, [project, project, since, since, limit])
    _emit(cols, rows, fmt)


# --------------------------------------------------------------------------- #
# cost
# --------------------------------------------------------------------------- #
@cli.command()
@click.option(
    "--by",
    type=click.Choice(["project", "model", "day", "session"]),
    default="project",
    help="Cost rollup dimension.",
)
@click.option("--since", help="Only count usage on/after this date (YYYY-MM-DD).")
@click.option("--limit", "-n", default=30, help="Max rows.")
@_fmt_option
@click.pass_context
def cost(ctx: click.Context, by: str, since: str | None, limit: int, fmt: str) -> None:
    """Estimated cost rollups. Main-loop only - subagent spend is unpriced (see `agents`)."""
    since_pred = "(? IS NULL OR ts >= TRY_CAST(? AS TIMESTAMP))"
    params: list[object]
    if by == "model":
        sql = f"""
            SELECT model, count(*) AS turns,
                   sum(input_tokens + cache_creation_tokens + cache_read_tokens) AS in_tok,
                   sum(output_tokens) AS out_tok, round(sum(cost_usd), 2) AS cost_usd
            FROM message_usage WHERE {since_pred}
            GROUP BY 1 ORDER BY cost_usd DESC LIMIT ?
        """
        params = [since, since, limit]
    elif by == "day":
        sql = f"""
            SELECT CAST(ts AS DATE) AS day, count(*) AS turns,
                   sum(output_tokens) AS out_tok, round(sum(cost_usd), 2) AS cost_usd
            FROM message_usage WHERE {since_pred} AND ts IS NOT NULL
            GROUP BY 1 ORDER BY day DESC LIMIT ?
        """
        params = [since, since, limit]
    elif by == "session":
        sql = """
            SELECT left(session_id, 8) AS id, project, started_at,
                   output_tokens AS out_tok, round(cost_usd, 2) AS cost_usd
            FROM sessions WHERE (? IS NULL OR started_at >= TRY_CAST(? AS TIMESTAMP))
            ORDER BY cost_usd DESC LIMIT ?
        """
        params = [since, since, limit]
    else:  # project
        sql = f"""
            SELECT project, count(DISTINCT session_id) AS sessions,
                   sum(output_tokens) AS out_tok, round(sum(cost_usd), 2) AS cost_usd
            FROM message_usage WHERE {since_pred}
            GROUP BY 1 ORDER BY cost_usd DESC LIMIT ?
        """
        params = [since, since, limit]
    _emit(*db.query(_con(ctx), sql, params), fmt=fmt)


# --------------------------------------------------------------------------- #
# tools
# --------------------------------------------------------------------------- #
@cli.command()
@click.option("--project", "-p", help="Filter to projects matching this.")
@click.option("--bash", is_flag=True, help="Break down Bash invocations by leading command.")
@click.option("--limit", "-n", default=30, help="Max rows.")
@_fmt_option
@click.pass_context
def tools(ctx: click.Context, project: str | None, bash: bool, limit: int, fmt: str) -> None:
    """Tool-use frequency across your history (optionally a Bash command breakdown)."""
    proj_pred = "(? IS NULL OR project ILIKE '%' || ? || '%')"
    if bash:
        sql = f"""
            SELECT lower(split_part(trim(tool_input->>'$.command'), ' ', 1)) AS command,
                   count(*) AS calls, count(DISTINCT session_id) AS sessions
            FROM tool_calls
            WHERE tool_name = 'Bash' AND {proj_pred}
            GROUP BY 1 ORDER BY calls DESC LIMIT ?
        """
    else:
        sql = f"""
            SELECT tool_name, count(*) AS calls,
                   count(DISTINCT session_id) AS sessions,
                   count(DISTINCT project) AS projects
            FROM tool_calls WHERE {proj_pred}
            GROUP BY 1 ORDER BY calls DESC LIMIT ?
        """
    _emit(*db.query(_con(ctx), sql, [project, project, limit]), fmt=fmt)


# --------------------------------------------------------------------------- #
# errors
# --------------------------------------------------------------------------- #
@cli.command()
@click.option("--project", "-p", help="Filter to projects matching this.")
@click.option("--list", "list_", is_flag=True, help="List recent error events instead of a rollup.")
@click.option("--limit", "-n", default=30, help="Max rows.")
@_fmt_option
@click.pass_context
def errors(ctx: click.Context, project: str | None, list_: bool, limit: int, fmt: str) -> None:
    """API errors and retries (rate limits, transient failures), by project/status."""
    proj_pred = "(? IS NULL OR project ILIKE '%' || ? || '%')"
    if list_:
        sql = f"""
            SELECT left(session_id, 8) AS id, project, ts, status, model
            FROM errors WHERE {proj_pred}
            ORDER BY ts DESC NULLS LAST LIMIT ?
        """
    else:
        sql = f"""
            SELECT project, status, count(*) AS hits,
                   count(DISTINCT session_id) AS sessions
            FROM errors WHERE {proj_pred}
            GROUP BY 1, 2 ORDER BY hits DESC LIMIT ?
        """
    _emit(*db.query(_con(ctx), sql, [project, project, limit]), fmt=fmt)


# --------------------------------------------------------------------------- #
# agents
# --------------------------------------------------------------------------- #
@cli.command()
@click.option(
    "--by",
    type=click.Choice(["type", "model", "session", "project"]),
    default="type",
    help="Group subagent dispatches by this dimension.",
)
@click.option("--limit", "-n", default=30, help="Max rows.")
@_fmt_option
@click.pass_context
def agents(ctx: click.Context, by: str, limit: int, fmt: str) -> None:
    """Subagent (Agent tool) dispatches and their token totals (unpriced)."""
    dim = {
        "type": "subagent_type",
        "model": "model",
        "session": "left(session_id, 8)",
        "project": "project",
    }[by]
    sql = f"""
        SELECT {dim} AS {by}, count(*) AS dispatches,
               sum(subagent_tokens) AS subagent_tokens,
               CAST(round(avg(subagent_tokens)) AS BIGINT) AS avg_tokens
        FROM agents
        GROUP BY 1 ORDER BY dispatches DESC NULLS LAST LIMIT ?
    """
    _emit(*db.query(_con(ctx), sql, [limit]), fmt=fmt)


# --------------------------------------------------------------------------- #
# session <id>
# --------------------------------------------------------------------------- #
@cli.command()
@click.argument("session_id")
@click.option("--limit", "-n", default=80, help="Max timeline rows.")
@click.pass_context
def session(ctx: click.Context, session_id: str, limit: int) -> None:
    """Show one session's decision timeline (prompts, tool calls, errors) by id prefix."""
    con = _con(ctx)
    _, rows = db.query(
        con,
        "SELECT session_id, project, git_branch, started_at, ended_at, duration_min, "
        "messages, models, round(cost_usd, 2) FROM sessions "
        "WHERE session_id LIKE ? || '%' LIMIT 2",
        [session_id],
    )
    if not rows:
        click.echo(f"No session matching prefix {session_id!r}.", err=True)
        sys.exit(1)
    if len(rows) > 1:
        click.echo(f"Prefix {session_id!r} is ambiguous - be more specific.", err=True)
        sys.exit(1)
    full_id, project, branch, started, ended, dur, msgs, models, usd = rows[0]
    click.echo(f"session  {full_id}")
    click.echo(f"project  {project}   branch {branch}")
    click.echo(f"span     {started} -> {ended}  ({dur} min, {msgs} messages)")
    model_list = models if isinstance(models, list) else []
    click.echo(f"models   {', '.join(str(m) for m in model_list)}")
    click.echo(f"est cost ${usd}")
    click.echo("-" * 60)
    timeline_sql = """
        SELECT ts, 'prompt' AS kind,
               left(regexp_replace(text, '\\s+', ' ', 'g'), 110) AS detail
        FROM prompts WHERE session_id = ? AND kind = 'prompt'
        UNION ALL
        SELECT ts, 'tool:' || tool_name,
               left(coalesce(tool_input->>'$.command', tool_input->>'$.file_path',
                             tool_input->>'$.description', tool_input->>'$.pattern',
                             tool_input->>'$.query', tool_input->>'$.subagent_type', ''), 110)
        FROM tool_calls WHERE session_id = ?
        UNION ALL
        SELECT ts, 'ERROR ' || status, coalesce(model, '')
        FROM errors WHERE session_id = ?
        ORDER BY ts NULLS FIRST
        LIMIT ?
    """
    tcols, trows = db.query(con, timeline_sql, [full_id, full_id, full_id, limit])
    _emit(tcols, trows, "table")


# --------------------------------------------------------------------------- #
# search
# --------------------------------------------------------------------------- #
@cli.command()
@click.argument("text")
@click.option("--limit", "-n", default=25, help="Max matching sessions.")
@_fmt_option
@click.pass_context
def search(ctx: click.Context, text: str, limit: int, fmt: str) -> None:
    """Full-text search your typed prompts and session titles; returns matching sessions."""
    sql = """
        SELECT left(p.session_id, 8) AS id, s.project, s.started_at,
               count(*) AS hits,
               left(regexp_replace(any_value(p.text), '\\s+', ' ', 'g'), 90) AS sample
        FROM prompts p LEFT JOIN sessions s ON s.session_id = p.session_id
        WHERE lower(p.text) LIKE '%' || lower(?) || '%'
        GROUP BY 1, 2, 3 ORDER BY s.started_at DESC NULLS LAST LIMIT ?
    """
    _emit(*db.query(_con(ctx), sql, [text, limit]), fmt=fmt)


# --------------------------------------------------------------------------- #
# sql
# --------------------------------------------------------------------------- #
@cli.command(name="sql")
@click.argument("query")
@_fmt_option
@click.pass_context
def sql_cmd(ctx: click.Context, query: str, fmt: str) -> None:
    """Run an arbitrary read-only SQL SELECT over the views (sessions, message_usage,
    tool_calls, errors, agents, agent_results, events, prompts, model_pricing).
    """
    try:
        cols, rows = db.run_read_only(_con(ctx), query)
    except db.UnsafeSQLError as exc:
        click.echo(f"Refused: {exc}", err=True)
        sys.exit(2)
    _emit(cols, rows, fmt)


# --------------------------------------------------------------------------- #
# serve (thin local web viewer)
# --------------------------------------------------------------------------- #
@cli.command()
@click.option("--port", "-p", default=8787, help="Port to bind on localhost.")
@click.pass_context
def serve(ctx: click.Context, port: int) -> None:
    """Launch a local web dashboard over your history (localhost only)."""
    from ccq.server import serve as make_server  # noqa: PLC0415 - optional/lazy import

    httpd = make_server(_con(ctx), port=port)
    url = f"http://127.0.0.1:{port}/"
    click.echo(f"ccq viewer on {url}  (Ctrl-C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        click.echo("\nstopped.")
    finally:
        httpd.server_close()


# --------------------------------------------------------------------------- #
# cache (materialized snapshot for --fast)
# --------------------------------------------------------------------------- #
@cli.group()
def cache() -> None:
    """Manage the materialized snapshot that powers --fast."""


@cache.command(name="build")
@click.pass_context
def cache_build(ctx: click.Context) -> None:
    """(Re)build the snapshot from the transcripts."""
    path = db.build_cache(ctx.obj["projects_dir"])
    size_mb = path.stat().st_size / 1e6
    click.echo(f"Built snapshot: {path} ({size_mb:.0f} MB)")


@cache.command(name="status")
def cache_status() -> None:
    """Show snapshot location, size, and age relative to the newest transcript."""
    path = db.default_cache_path()
    if not path.exists():
        click.echo(f"No snapshot at {path}. Build one with `ccq cache build`.")
        return
    stale = db.is_cache_stale()
    size_mb = path.stat().st_size / 1e6
    click.echo(f"snapshot : {path} ({size_mb:.0f} MB)")
    click.echo(f"status   : {'STALE - transcripts changed since build' if stale else 'up to date'}")


@cache.command(name="path")
def cache_path() -> None:
    """Print the snapshot path."""
    click.echo(str(db.default_cache_path()))


@cache.command(name="clear")
def cache_clear() -> None:
    """Delete the snapshot."""
    path = db.default_cache_path()
    if path.exists():
        path.unlink()
        click.echo(f"Removed {path}")
    else:
        click.echo("No snapshot to remove.")


def main() -> None:
    """Console-script entry point."""
    cli(obj={})


if __name__ == "__main__":
    main()
