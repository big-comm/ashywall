#!/usr/bin/env bash
# Run AshyWall from the source tree without installing it.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
export ASHYWALL_LOCALE_DIR="$ROOT/usr/share/locale"
export ASHYWALL_ICON_DIR="$ROOT/usr/share/icons"
# Keep bytecode out of the source tree.
export PYTHONPYCACHEPREFIX="${XDG_CACHE_HOME:-$HOME/.cache}/ashywall/pycache"

exec python3 -m ashywall "$@"
