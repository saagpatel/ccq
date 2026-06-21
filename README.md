# ccq: query your own Claude Code agent history

[![CI](https://img.shields.io/github/actions/workflow/status/saagpatel/ccq/ci.yml?style=flat-square&logo=githubactions&logoColor=white&label=CI)](https://github.com/saagpatel/ccq/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.12%2B-blue?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)

`ccq` makes your local Claude Code transcripts queryable. It runs **DuckDB directly
over the JSONL** at `~/.claude/projects/<project>/<session>.jsonl`. No copy, no ETL,
no database to maintain. The transcripts are only ever **read**, never written.

Ask it where your tokens go, which tools you lean on, where runs hit rate limits,
how much you delegate to subagents, and what happened inside any single session.

```
$ ccq cost --by model
model                       turns          in_tok      out_tok   cost_usd
-------------------------  ------  --------------  -----------  ---------
claude-opus-4-8            12,345   3,456,789,012   28,500,000   4,210.55
claude-sonnet-4-6           6,789     901,234,567    8,400,000      612.30
...
```

A few more angles: your most-used tools, and the full timeline of any one session.

```
$ ccq tools
tool_name    calls  sessions  projects
----------  ------  --------  --------
Bash         4,210       210       48
Read         2,980       198       45
Edit         1,740       142       40
Agent          410        88       24
```

```
$ ccq session a1b2c3d4
session  a1b2c3d4-9f02-4c7e-bb31-0e5a6d7c8e90
project  demo-app   branch main
span     2026-06-01 10:00 -> 10:42  (42 min, 88 messages)
models   claude-opus-4-8, claude-sonnet-4-6
est cost $4.21
------------------------------------------------------------
ts          kind        detail
10:00:05    prompt      refactor the duckdb loader
10:01:12    tool:Bash   uv run pytest -q
10:03:48    tool:Edit   src/ccq/db.py
10:07:20    ERROR 429   claude-opus-4-8
```

## Install

```bash
uv sync          # create the venv + install deps
uv run ccq --help
```

(Or `uv tool install .` to put `ccq` on your PATH.)

## Commands

| Command | What it answers |
|---|---|
| `ccq sessions` | List sessions: project, span, message count, tokens, estimated cost. `--sort cost\|duration\|messages\|recent`, `--project`, `--since`, `-n`. |
| `ccq cost` | Cost rollups. `--by project\|model\|day\|session`. Main-loop only (see caveat). |
| `ccq tools` | Tool-use frequency. `--bash` breaks Bash calls down by leading command. |
| `ccq errors` | API errors / retries (429s, etc.) by project + status. `--list` for recent events. |
| `ccq agents` | Subagent (Agent tool) dispatches + token totals. `--by type\|model\|session\|project`. |
| `ccq session <id-prefix>` | One session's decision timeline: prompts, tool calls, errors, in order. |
| `ccq search <text>` | Full-text over your typed prompts and session titles → matching sessions. |
| `ccq sql "<SELECT…>"` | Run an arbitrary **read-only** query over the views (power surface). |
| `ccq serve` | Launch a local web dashboard (localhost only) with a read-only SQL box. |
| `ccq cache build\|status\|clear` | Manage the materialized snapshot that powers `--fast`. |

Every command takes `-f table|json|csv` and a global `--projects-dir` (defaults to
`~/.claude/projects`, handy for pointing at a backup).

## Fast mode

A live query rescans ~1 GB of JSONL each time (~1-3 s). For instant repeat queries,
materialize a snapshot once and pass `--fast` (`-F`):

```bash
ccq cache build          # ~6 s, writes a ~100 MB snapshot to ~/.cache/ccq
ccq -F cost --by model   # now ~0.1 s
ccq cache status         # shows STALE once transcripts change; rebuild to refresh
```

The snapshot lives under `$XDG_CACHE_HOME` (never in `~/.claude`), is opened
**read-only** at the engine level, and `--fast` builds it automatically on first use.

## Web viewer

```bash
ccq -F serve             # http://127.0.0.1:8787  (Ctrl-C to stop)
```

A dashboard (cost by model, tools, errors, subagents, priciest sessions) plus a SQL
box that runs the same read-only-guarded queries. **Drill down:** click a project
chip to filter to that project, or a session id to see its full decision timeline.
Standard-library `http.server`, no extra dependencies, binds to localhost only.

## The query surface (`ccq sql`)

`sql` exposes these views. Compose your own:

- **`sessions`** - one row per session: project, branch, span, `messages`, `models`, tokens, `cost_usd`.
- **`message_usage`** - one row per assistant turn: token breakdown + `cost_usd`.
- **`tool_calls`** - one row per tool invocation: `tool_name`, `tool_input` (JSON).
- **`errors`** - API error events: `status`, project, session, model.
- **`agents`** / **`agent_results`** - subagent dispatches joined to their `subagent_tokens`.
- **`prompts`** - searchable typed prompts + session titles.
- **`events`** - the raw per-line view everything else is built on.
- **`model_pricing`** - the per-model rate table used for costing.

```bash
ccq sql "SELECT project, round(sum(cost_usd),2) usd
         FROM message_usage WHERE ts >= DATE '2026-06-01'
         GROUP BY 1 ORDER BY usd DESC"
```

`sql` accepts a **single read-only statement** (SELECT/WITH/EXPLAIN/…). Writes,
`ATTACH`, `COPY`, `INSTALL`, and multi-statement input are refused. Tip: DuckDB
reserves words like `day`, `first`, `last`, so quote them if used as aliases (`AS "day"`).

## How it works

`read_ndjson_objects('~/.claude/projects/*/*.jsonl')` loads each line as an opaque
`JSON` value (zero schema inference, since the records are heterogeneous), and SQL views
extract the entities. Two things the transcripts taught us are baked in:

- Numeric fields use `TRY_CAST` (heterogeneous lines otherwise break a hard cast).
- Token-casting views read from a type-filtered subquery so the optimizer can't
  reorder a cast ahead of the `type = 'assistant'` filter.

## Cost is estimated, and main-loop only

Transcripts store **token counts, not dollars**. `ccq` prices them with published
per-million-token rates (`src/ccq/pricing.py`) and the standard cache multipliers
(cache write 1.25×, cache read 0.10×). Two honest caveats:

1. **Estimates, not invoices**: unknown/`<synthetic>` models price to `$0`.
2. **Main-loop only**: a subagent's spend is **not** in the transcript. The only
   signal that survives is `toolUseResult.totalTokens` (no input/output split, so it
   can't be priced). `ccq agents` surfaces those token totals **separately**; they
   are never folded into a dollar figure.

## Develop

```bash
uv run pytest                                   # tests over synthetic fixtures
uv run ruff check . && uv run ruff format .     # lint + format
uv run ty check src/                            # type check
```

Read-only on `~/.claude/projects` by contract. The test suite uses synthetic
fixtures and never reads your real history.
