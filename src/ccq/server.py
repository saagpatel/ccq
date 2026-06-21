"""A thin local web viewer over the ccq views. Standard library only.

`ccq serve` starts this on 127.0.0.1. It renders an overview dashboard and a
read-only SQL box (guarded by the same `assert_read_only` as the CLI). It binds
to localhost and only ever issues read-only queries.
"""

from __future__ import annotations

import html
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, quote, urlparse

from ccq import db

if TYPE_CHECKING:
    import duckdb

# Timeline for one session: prompts + tool calls + errors, ordered.
_TIMELINE_SQL = """
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
    ORDER BY ts NULLS FIRST LIMIT 200
"""

_CSS = """
:root { color-scheme: light dark; }
body { font: 14px/1.5 -apple-system, system-ui, sans-serif; margin: 0; padding: 2rem;
       max-width: 1100px; margin-inline: auto; }
h1 { font-size: 1.4rem; margin: 0 0 .25rem; }
h2 { font-size: 1rem; margin: 2rem 0 .5rem; color: #888; text-transform: uppercase;
     letter-spacing: .05em; }
.sub { color: #888; margin-top: 0; }
table { border-collapse: collapse; width: 100%; margin-bottom: .5rem; font-variant-numeric: tabular-nums; }
th, td { text-align: left; padding: .35rem .6rem; border-bottom: 1px solid #8884; }
th { color: #888; font-weight: 600; }
td.num, th.num { text-align: right; }
.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 1.5rem; }
form { margin: 1rem 0; }
textarea { width: 100%; font: 13px ui-monospace, monospace; padding: .6rem; box-sizing: border-box;
           border: 1px solid #8886; border-radius: 6px; min-height: 4rem; }
button { margin-top: .5rem; padding: .4rem 1rem; border-radius: 6px; border: 1px solid #8886;
         cursor: pointer; }
.err { color: #c33; font-family: ui-monospace, monospace; }
code { background: #8882; padding: .1rem .3rem; border-radius: 3px; }
"""

_NUMERIC_HINT = (
    "usd",
    "tok",
    "tokens",
    "calls",
    "hits",
    "dispatches",
    "sessions",
    "msgs",
    "messages",
    "min",
    "n",
    "turns",
    "projects",
    "avg_tokens",
)


def _is_num_col(name: str) -> bool:
    low = name.lower()
    return any(low == h or low.endswith((f"_{h}", h)) for h in _NUMERIC_HINT)


def _table(columns: list[str], rows: list[tuple[object, ...]], link: str | None = None) -> str:
    """Render an HTML table. If *link* is a format string (one ``{}``), the first
    column's cell becomes a link with the (URL-encoded) value substituted in."""
    if not rows:
        return "<p class='sub'>(no rows)</p>"
    num = [_is_num_col(c) for c in columns]
    head = "".join(
        f"<th class='num'>{html.escape(c)}</th>" if num[i] else f"<th>{html.escape(c)}</th>"
        for i, c in enumerate(columns)
    )
    body = []
    for r in rows:
        cells = []
        for i, v in enumerate(r):
            text = "" if v is None else html.escape(str(v))
            if i == 0 and link and v is not None:
                href = html.escape(link.format(quote(str(v))))
                text = f"<a href='{href}'>{text}</a>"
            cells.append(f"<td class='num'>{text}</td>" if num[i] else f"<td>{text}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def _run(
    con: duckdb.DuckDBPyConnection,
    sql: str,
    params: list[object] | None = None,
    link: str | None = None,
) -> str:
    """Execute an internal query and render it as a table; errors render inline.

    If *link* is given, the first column links via that format string.
    """
    try:
        cur = con.execute(sql, params) if params else con.execute(sql)
        return _table([d[0] for d in cur.description], cur.fetchall(), link=link)
    except Exception as exc:  # noqa: BLE001 - never 500 the page on a query error
        return f"<p class='err'>{html.escape(str(exc))}</p>"


def _page(body: str) -> bytes:
    return (
        f"<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>ccq</title><style>{_CSS}</style></head><body>{body}</body></html>"
    ).encode()


_SECTIONS: tuple[tuple[str, str], ...] = (
    (
        "Cost by model",
        "SELECT model, count(*) AS turns, round(sum(cost_usd),2) AS cost_usd "
        "FROM message_usage GROUP BY 1 ORDER BY cost_usd DESC",
    ),
    (
        "Top projects by cost",
        "SELECT project, count(DISTINCT session_id) AS sessions, "
        "round(sum(cost_usd),2) AS cost_usd FROM message_usage "
        "GROUP BY 1 ORDER BY cost_usd DESC LIMIT 10",
    ),
    (
        "Top tools",
        "SELECT tool_name, count(*) AS calls FROM tool_calls "
        "GROUP BY 1 ORDER BY calls DESC LIMIT 10",
    ),
    (
        "Subagents by type",
        "SELECT subagent_type, count(*) AS dispatches, "
        "sum(subagent_tokens) AS subagent_tokens FROM agents "
        "GROUP BY 1 ORDER BY dispatches DESC LIMIT 8",
    ),
    (
        "Errors by project",
        "SELECT project, status, count(*) AS hits FROM errors "
        "GROUP BY 1,2 ORDER BY hits DESC LIMIT 8",
    ),
)


_SESSION_LINK = "/session?id={}"

# Cap rows fetched + rendered for an ad-hoc SQL-box query, so an unbounded
# `SELECT * FROM events` cannot exhaust memory or freeze the browser tab.
_MAX_RENDER_ROWS = 1000


def _project_chips(con: duckdb.DuckDBPyConnection) -> str:
    """A row of project links (top projects by cost) for drill-down filtering."""
    try:
        rows = con.execute(
            "SELECT project FROM message_usage GROUP BY 1 ORDER BY sum(cost_usd) DESC LIMIT 14"
        ).fetchall()
    except Exception:  # noqa: BLE001
        return ""
    chips = " ".join(
        f"<a href='/?project={quote(str(p))}'><code>{html.escape(str(p))}</code></a>"
        for (p,) in rows
        if p is not None
    )
    return f"<p class='sub'>filter by project: {chips}</p>"


def _sql_box(query: str | None) -> str:
    return (
        "<form action='/q' method='get'>"
        "<textarea name='sql' placeholder='SELECT ... (read-only)'>"
        f"{html.escape(query or '')}</textarea><br>"
        "<button type='submit'>Run SQL</button> "
        "<span class='sub'>views: sessions, message_usage, tool_calls, errors, agents, prompts</span>"
        "</form>"
    )


def _header(subtitle: str = "your Claude Code agent history") -> str:
    return f"<h1><a href='/'>ccq</a></h1><p class='sub'>{html.escape(subtitle)}</p>"


def _dashboard(
    con: duckdb.DuckDBPyConnection, query: str | None, project: str | None = None
) -> str:
    if project:
        return _project_panel(con, project)
    parts = [_header(), _sql_box(query)]
    if query:
        try:
            cols, rows = db.run_read_only(con, query, limit=_MAX_RENDER_ROWS)
            if len(rows) > _MAX_RENDER_ROWS:
                shown = rows[:_MAX_RENDER_ROWS]
                heading = f"Result (first {_MAX_RENDER_ROWS:,} rows; add a LIMIT to narrow)"
            else:
                shown = rows
                heading = f"Result ({len(rows):,} rows)"
            parts.append(f"<h2>{html.escape(heading)}</h2>{_table(cols, shown)}")
        except db.UnsafeSQLError as exc:
            parts.append(f"<p class='err'>Refused: {html.escape(str(exc))}</p>")
        except Exception as exc:  # noqa: BLE001 - surface any DuckDB error to the page
            parts.append(f"<p class='err'>{html.escape(str(exc))}</p>")
    parts.append(_project_chips(con))
    parts.append("<h2>Most expensive sessions</h2>")
    parts.append(
        _run(
            con,
            "SELECT left(session_id,8) AS id, project, round(cost_usd,2) AS cost_usd, "
            "messages FROM sessions ORDER BY cost_usd DESC LIMIT 10",
            link=_SESSION_LINK,
        )
    )
    parts.append("<div class='grid'>")
    for title, sql in _SECTIONS:
        parts.append(f"<div><h2>{html.escape(title)}</h2>{_run(con, sql)}</div>")
    parts.append("</div>")
    return "".join(parts)


def _project_panel(con: duckdb.DuckDBPyConnection, project: str) -> str:
    """A project-focused view: its sessions, cost by model, and top tools."""
    parts = [
        _header(f"project: {project}"),
        "<h2>Sessions</h2>",
        _run(
            con,
            "SELECT left(session_id,8) AS id, started_at, round(cost_usd,2) AS cost_usd, "
            "messages FROM sessions WHERE project = ? ORDER BY started_at DESC LIMIT 25",
            [project],
            link=_SESSION_LINK,
        ),
        "<div class='grid'>",
        "<div><h2>Cost by model</h2>"
        + _run(
            con,
            "SELECT model, count(*) AS turns, round(sum(cost_usd),2) AS cost_usd "
            "FROM message_usage WHERE project = ? GROUP BY 1 ORDER BY cost_usd DESC",
            [project],
        )
        + "</div>",
        "<div><h2>Top tools</h2>"
        + _run(
            con,
            "SELECT tool_name, count(*) AS calls FROM tool_calls "
            "WHERE project = ? GROUP BY 1 ORDER BY calls DESC LIMIT 12",
            [project],
        )
        + "</div>",
        "</div>",
    ]
    return "".join(parts)


def _session_view(con: duckdb.DuckDBPyConnection, prefix: str) -> str:
    """One session's header + decision timeline, resolved by id prefix."""
    try:
        rows = con.execute(
            "SELECT session_id, project, git_branch, started_at, ended_at, duration_min, "
            "messages, round(cost_usd,2) FROM sessions WHERE session_id LIKE ? || '%' LIMIT 2",
            [prefix],
        ).fetchall()
    except Exception as exc:  # noqa: BLE001
        return _header() + f"<p class='err'>{html.escape(str(exc))}</p>"
    if not rows:
        return _header() + f"<p class='err'>No session matching prefix {html.escape(prefix)}.</p>"
    if len(rows) > 1:
        return _header() + f"<p class='err'>Prefix {html.escape(prefix)} is ambiguous.</p>"
    sid, proj, branch, _started, _ended, dur, msgs, cost = rows[0]
    proj_link = f"/?project={quote(str(proj))}" if proj else "/"
    meta = (
        f"<p class='sub'>project <a href='{html.escape(proj_link)}'>{html.escape(str(proj))}</a> "
        f"&middot; branch {html.escape(branch or '(no branch)')} &middot; {html.escape(str(msgs))} messages "
        f"&middot; {html.escape(str(dur))} min &middot; est ${html.escape(str(cost))}</p>"
    )
    timeline = _run(con, _TIMELINE_SQL, [sid, sid, sid])  # timeline rows are not linked
    return f"{_header('session ' + str(sid))}{meta}<h2>Timeline</h2>{timeline}"


def make_handler(con: duckdb.DuckDBPyConnection) -> type[BaseHTTPRequestHandler]:
    """Build a request handler bound to a DuckDB connection."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - parent API
            """Silence default request logging."""
            del format, args

        def _send(self, body: bytes, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            if parsed.path == "/session":
                sid = params.get("id", [""])[0]
                self._send(_page(_session_view(con, sid)))
            elif parsed.path in ("/", "/q"):
                query = params.get("sql", [None])[0]
                project = params.get("project", [None])[0]
                self._send(_page(_dashboard(con, query, project)))
            else:
                self._send(_page(_header() + "<p class='err'>404</p>"), status=404)

    return Handler


def serve(con: duckdb.DuckDBPyConnection, host: str = "127.0.0.1", port: int = 8787) -> HTTPServer:
    """Create (but do not start) the HTTP server bound to *con*."""
    return HTTPServer((host, port), make_handler(con))
