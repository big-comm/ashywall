#!/usr/bin/env bash
# Run the same local checks used by CI, without opening the application.
set -euo pipefail

translation_args=()
case "${1:-}" in
    "") ;;
    --release) translation_args=(--require-complete) ;;
    *) printf 'Usage: %s [--release]\n' "$0" >&2; exit 2 ;;
esac
if (( $# > 1 )); then
    printf 'Usage: %s [--release]\n' "$0" >&2
    exit 2
fi

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

validation_dir="$(mktemp -d "${TMPDIR:-/tmp}/ashywall-validation.XXXXXXXX")"
trap 'rm -rf -- "$validation_dir"' EXIT
export GSETTINGS_BACKEND=memory
export XDG_CONFIG_HOME="$validation_dir/config"
export XDG_DATA_HOME="$validation_dir/data"
export XDG_CACHE_HOME="$validation_dir/cache"
export XDG_STATE_HOME="$validation_dir/state"

python -m pytest
ruff check src tests scripts/check-translations.py
python scripts/check-translations.py "${translation_args[@]}"
for po in locale/*.po; do
    language="$(basename -- "$po" .po)"
    msgfmt --check -o "$validation_dir/$language.mo" "$po"
    cmp -- "$validation_dir/$language.mo" \
        "usr/share/locale/$language/LC_MESSAGES/ashywall.mo"
done
desktop-file-validate usr/share/applications/org.communitybig.ashywall.desktop
appstreamcli validate --no-net usr/share/metainfo/org.communitybig.ashywall.metainfo.xml
python -m build --wheel --no-isolation --outdir "$validation_dir/wheel"
