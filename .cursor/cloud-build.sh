#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

# Keep the Cloud Agent build on the repository's declared Python and lockfile.
# The Cloud base image does not guarantee uv, so bootstrap the exact release only
# when a matching executable is not already available. The versioned Astral
# installer verifies the release checksum; UV_UNMANAGED_INSTALL avoids profile
# edits and self-updaters in the build VM.
uv_version="0.12.12"
uv_install_dir="${HOME}/.local/bin"
uv_bin=""

if command -v uv >/dev/null 2>&1; then
  candidate_uv="$(command -v uv)"
  candidate_version="$("$candidate_uv" --version | awk '{print $2}')"
  if [ "$candidate_version" = "$uv_version" ]; then
    uv_bin="$candidate_uv"
  fi
fi

if [ -z "$uv_bin" ]; then
  uv_bin="$uv_install_dir/uv"
  if [ ! -x "$uv_bin" ] || [ "$("$uv_bin" --version | awk '{print $2}')" != "$uv_version" ]; then
    mkdir -p "$uv_install_dir"
    curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
      "https://astral.sh/uv/${uv_version}/install.sh" \
      | UV_UNMANAGED_INSTALL="$uv_install_dir" sh
  fi
fi

if [ ! -x "$uv_bin" ] || [ "$("$uv_bin" --version | awk '{print $2}')" != "$uv_version" ]; then
  echo "expected uv ${uv_version} at ${uv_bin}" >&2
  exit 1
fi

"$uv_bin" python install 3.12
"$uv_bin" sync --all-groups --frozen

# The build is intentionally self-contained: no project MCPs, services, secrets,
# or live transcript data are needed for these deterministic gates.
"$uv_bin" run ruff format --check .
"$uv_bin" run ruff check .
"$uv_bin" run ty check src/
"$uv_bin" run pytest -q
