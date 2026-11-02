#!/usr/bin/env bash
# Run from this checkout, including when the workspace path differs.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
exec uv run --no-sync python -m people_archive.refresh "$@"
