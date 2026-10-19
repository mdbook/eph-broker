#!/usr/bin/env bash
# Reinstall the broker's hermes venv NON-editable.
#
# WHY: the interpreter-path tripwire (adapters/hermes.py PATH_GUARD) refuses every hermes turn whose
# interpreter has a relative sys.path entry. An EDITABLE install (`uv sync` / `pip install -e`, uv's own
# default) adds exactly such an entry (an `__editable__.<dist>.finder.__path_hook__` placeholder), which with
# cwd = a worker worktree would let a worker-planted module shadow hermes itself. A routine `uv sync` in
# ~/.local/share/eph-hermes/src without --no-editable SILENTLY RECREATES that entry. Nothing provisions or
# runs this automatically, so it is a MANUAL step: run it again any time the checkout is re-synced. The script
# keeps the reproducible command in the repo so "how do I fix a re-editabled hermes venv" isn't tribal knowledge.
#
# Getting this wrong fails CLOSED, not open: the tripwire refuses every turn until this is re-run. It reads as
# "hermes broken", not a security hole.
#
# Usage: scripts/hermes-venv-reinstall.sh [SRC_DIR] [VENV_DIR]
#   SRC_DIR  defaults to ~/.local/share/eph-hermes/src  (the hermes checkout; untouched by this script)
#   VENV_DIR defaults to ~/.local/share/eph-hermes/venv (the broker's private hermes venv)
#
# Verify after running: `python -c "import json; print(json.load(open('<VENV_DIR>/lib*/python*/site-packages/'
#   'hermes_agent-*.dist-info/direct_url.json'))['dir_info']['editable'])"` must print False, and there must be
# no `__editable__*` file under site-packages.

set -euo pipefail

SRC_DIR="${1:-$HOME/.local/share/eph-hermes/src}"
VENV_DIR="${2:-$HOME/.local/share/eph-hermes/venv}"

if [ ! -d "$SRC_DIR" ]; then
    echo "hermes-venv-reinstall: no such SRC_DIR: $SRC_DIR" >&2
    exit 1
fi
if [ ! -d "$VENV_DIR" ]; then
    echo "hermes-venv-reinstall: no such VENV_DIR: $VENV_DIR" >&2
    exit 1
fi

cd "$SRC_DIR"

# HERMES_NIX_BUILD=1: upstream's setup.py refuses `bdist_wheel` unless set (the uv2nix sealed-wheel switch;
#   build-time only, nothing reads it at runtime). Without it, --no-editable fails to build.
# --frozen: install exactly what uv.lock pins, no re-resolve.
# --inexact: don't uninstall packages outside the lock (e.g. boto3/botocore/jmespath/s3transfer, installed
#   after the original `uv sync` for the bedrock/s3 backends). A plain sync would remove them.
# --extra anthropic: the broker's hermes adapter needs the `anthropic` SDK for the `anthropic_messages`
#   transport (adapters/hermes.py; the live adapter uses chat_completions, so its absence stays silent until
#   then). `anthropic` is an OPTIONAL extra in the hermes pyproject (pinned `anthropic==0.87.0`), and a plain
#   `uv sync` leaves it out, so request it explicitly here. Do NOT hand-`uv pip install anthropic`: that
#   resolves the latest version, not the pinned one, and drifts from the lock.
HERMES_NIX_BUILD=1 UV_PROJECT_ENVIRONMENT="$VENV_DIR" \
    uv sync --no-editable --frozen --inexact --extra anthropic

# setuptools build artefact left in SRC_DIR by the wheel build; SRC_DIR itself is not needed at runtime once
# the wheel is installed (only direct_url.json metadata references it), but is kept around for the next
# rebuild.
rm -rf "$SRC_DIR/build/"

echo "hermes-venv-reinstall: done. Verify direct_url.json editable:false and no __editable__* in site-packages."
