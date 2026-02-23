#!/usr/bin/env bash
set -euo pipefail

###############################################################################
# Config
###############################################################################
ROOT_DIR="${ROOT_DIR:-$(pwd)}"
APP_DIR="${APP_DIR:-$ROOT_DIR/app}"

MAIN_PY="${MAIN_PY:-$APP_DIR/main.py}"
SETTINGS_PY="${SETTINGS_PY:-$APP_DIR/settings.py}"
DOCKERFILE="${DOCKERFILE:-$ROOT_DIR/Dockerfile}"
REQS_TXT="${REQS_TXT:-$ROOT_DIR/requirements.txt}"

###############################################################################
# Helpers
###############################################################################
die() { echo "ERROR: $*" >&2; exit 1; }
need_file() { [[ -f "$1" ]] || die "File not found: $1"; }

need_file "$MAIN_PY"
need_file "$SETTINGS_PY"
need_file "$DOCKERFILE"
need_file "$REQS_TXT"

###############################################################################
# Output
###############################################################################
echo "==> ROOT_DIR: $ROOT_DIR"
echo

echo "===== app/main.py ($MAIN_PY) ====="
cat "$MAIN_PY"
echo

echo "===== app/settings.py ($SETTINGS_PY) ====="
cat "$SETTINGS_PY"
echo

echo "===== Dockerfile ($DOCKERFILE) ====="
cat "$DOCKERFILE"
echo

echo "===== requirements.txt ($REQS_TXT) ====="
cat "$REQS_TXT"
echo
