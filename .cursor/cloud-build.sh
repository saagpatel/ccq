#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

# Keep the Cloud Agent build on the repository's declared Python and lockfile.
uv python install 3.12
uv sync --all-groups --frozen

# The build is intentionally self-contained: no project MCPs, services, secrets,
# or live transcript data are needed for these deterministic gates.
uv run ruff format --check .
uv run ruff check .
uv run ty check src/
uv run pytest -q
