#!/usr/bin/env bash
set -euo pipefail

MODE="build"
REPO="pypi"
SKIP_TESTS=0
SKIP_CHECK=0

usage() {
  cat <<'EOF'
Usage:
  ./build.sh [release|build|upload|help] [--repo <name>] [--skip-tests] [--skip-check]

Commands:
  release            Run tests, build packages, and check metadata; upload only when TWINE_UPLOAD=1.
  build              Run tests, build packages, and check metadata only.
  upload             Upload existing dist artifacts only.
  help               Show this help message.

Options:
  --repo <name>      Target twine repository (default: pypi).
  --skip-tests       Skip pytest step (applies to release/build).
  --skip-check       Skip twine check step.
  TWINE_UPLOAD=1     Enable upload during 'release' mode.
EOF
}

run_tests() {
  if [[ "${SKIP_TESTS}" == "1" ]]; then
    echo "Skipping tests."
    return
  fi
  uv run python3 -m pytest -q -m "not real"
}

build_dist() {
  rm -rf dist
  uv run python3 -m build
}

check_dist() {
  if ! compgen -G "dist/*" > /dev/null; then
    echo "No artifacts found in dist/. Run './build.sh build' first."
    exit 1
  fi
  if [[ "${SKIP_CHECK}" == "1" ]]; then
    echo "Skipping twine check."
    return
  fi
  uv run python3 -m twine check dist/*
}

upload_dist() {
  if ! compgen -G "dist/*" > /dev/null; then
    echo "No artifacts found in dist/. Run './build.sh build' first."
    exit 1
  fi
  uv run python3 -m twine upload --repository "${REPO}" dist/*
}

if [[ $# -gt 0 && "$1" != --* ]]; then
  MODE="$1"
  shift
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo)
      if [[ $# -lt 2 ]]; then
        echo "Missing value for --repo"
        exit 1
      fi
      REPO="$2"
      shift 2
      ;;
    --skip-tests)
      SKIP_TESTS=1
      shift
      ;;
    --skip-check)
      SKIP_CHECK=1
      shift
      ;;
    -h|--help)
      MODE="help"
      shift
      ;;
    *)
      echo "Unknown argument: $1"
      usage
      exit 1
      ;;
  esac
done

case "${MODE}" in
  release)
    run_tests
    build_dist
    check_dist
    if [[ "${TWINE_UPLOAD:-0}" == "1" ]]; then
      upload_dist
    else
      echo "Skipping upload in release mode. Set TWINE_UPLOAD=1 to upload."
    fi
    ;;
  build)
    run_tests
    build_dist
    check_dist
    ;;
  upload)
    check_dist
    upload_dist
    ;;
  help)
    usage
    ;;
  *)
    echo "Unknown command: ${MODE}"
    usage
    exit 1
    ;;
esac
