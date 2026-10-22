#!/usr/bin/env bash
# deploy/smoke_docker.sh - run the full adsops selftest in a throwaway container.
#
# The point is portability: everything adsops needs is stdlib, so a bare
# python:3.12-slim image is the whole environment. If the selftest passes
# there, it does not depend on this machine's Python, venv, or macOS.
#
# Usage:  deploy/smoke_docker.sh
# Exit:   docker's own exit code (0 = selftest passed).
set -euo pipefail

# PKG is this script's parent-of-parent dir (deploy/ -> repo root).
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PKG="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

if ! command -v docker >/dev/null 2>&1; then
    echo "smoke_docker.sh: docker not found on PATH" >&2
    exit 127
fi

exec docker run --rm -v "$PKG":/app -w /app python:3.12-slim python -m adsops --selftest
