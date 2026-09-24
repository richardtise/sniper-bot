#!/usr/bin/env bash
#
# Start the sniper bot, repairing the virtualenv if it is missing or broken.
#
# Why this is not just `source bot-env/bin/activate`: the venv is not committed
# (.gitignore), so a fresh clone, or a machine whose Python minor version differs
# from the one the venv was built with, leaves bot-env/bin/python pointing at a
# binary that no longer exists. That failed with "No such file or directory" and
# no hint as to why. Detect it and rebuild.
#
# Overridable:
#   VENV_DIR=bot-env   PYTHON_BIN=python3.12   SKIP_INSTALL=1
#
set -euo pipefail

cd "$(dirname "$0")"

VENV_DIR="${VENV_DIR:-bot-env}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
REQUIREMENTS="${REQUIREMENTS:-requirements.txt}"

die() { printf 'error: %s\n' "$*" >&2; exit 1; }

command -v "$PYTHON_BIN" >/dev/null 2>&1 \
  || die "'$PYTHON_BIN' not found. Install Python 3.10+ or set PYTHON_BIN=..."

# 1. The interpreter itself must run. A dangling symlink (the venv was built on
#    another machine) fails here.
if [ ! -x "$VENV_DIR/bin/python" ] || ! "$VENV_DIR/bin/python" -c '' >/dev/null 2>&1; then
  echo "==> '$VENV_DIR' is missing or its interpreter is broken; rebuilding with $PYTHON_BIN"
  rm -rf "$VENV_DIR"
  "$PYTHON_BIN" -m venv "$VENV_DIR" || die "failed to create venv with $PYTHON_BIN"
fi

# 2. Runtime imports must resolve. Checked separately from the interpreter so a
#    missing package triggers an install rather than a full rebuild.
if ! "$VENV_DIR/bin/python" -c 'import aiohttp, telegram, fastapi, dotenv' >/dev/null 2>&1; then
  if [ "${SKIP_INSTALL:-0}" = "1" ]; then
    die "'$VENV_DIR' is missing dependencies and SKIP_INSTALL=1"
  fi
  echo "==> Installing dependencies from $REQUIREMENTS"
  "$VENV_DIR/bin/python" -m pip install --upgrade pip
  "$VENV_DIR/bin/python" -m pip install -r "$REQUIREMENTS"
fi

if [ ! -f .env ]; then
  echo "warning: no .env found — copy .env.example to .env and fill it in" >&2
fi

echo "==> Starting bot"
exec "$VENV_DIR/bin/python" bot.py
