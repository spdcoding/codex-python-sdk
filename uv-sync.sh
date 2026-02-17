#!/usr/bin/env bash
set -euo pipefail

# Configure cache for faster local sync.
export UV_CACHE_DIR="${UV_CACHE_DIR:-$(pwd)/.uv-cache}"
# Default to public PyPI to avoid local mirror overrides.
export UV_INDEX_URL="${UV_INDEX_URL:-https://pypi.org/simple}"

# `uv sync` creates `.venv` if missing and installs the current project.
uv sync --extra test --extra build
