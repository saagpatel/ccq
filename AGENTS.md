<!-- portfolio-context:start -->
# Portfolio Context

## What This Project Is

ccq: `ccq` makes your local Claude Code transcripts queryable. It runs **DuckDB directly.

## Current State

Portfolio truth currently marks this project as `active` with `boilerplate` context. Phase 104 recovered minimum-viable context so future sessions can resume without rediscovery.

## Stack

- Primary stack: Python

## How To Run

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

## Known Risks

- This repo only has minimum-viable recovery context today; deeper handoff details may still live in the README and supporting docs.

## Next Recommended Move

Use this context plus the README and supporting docs to resume the next active task, then promote the repo beyond minimum-viable by capturing a dedicated handoff, roadmap, or discovery artifact.

<!-- portfolio-context:end -->
