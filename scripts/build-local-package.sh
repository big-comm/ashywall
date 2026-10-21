#!/usr/bin/env bash
# Build the reviewed local Git snapshot, without fetching the upstream source.
set -euo pipefail

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
    cat <<'HELP'
Usage: ./scripts/build-local-package.sh [makepkg options]

Build the current committed source into dist/packages using the project's
PKGBUILD. No upstream checkout is downloaded. Pass -si to install missing
dependencies and install the resulting package through pacman.

Commit source changes before building; ignored build artifacts are excluded.
HELP
    exit 0
fi

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

command -v makepkg >/dev/null || {
    printf '%s\n' 'makepkg is required to build an Arch/BigCommunity package.' >&2
    exit 1
}
if ! git diff --quiet HEAD -- || [[ -n "$(git ls-files --others --exclude-standard)" ]]; then
    printf '%s\n' 'Commit the source changes before building a local package.' >&2
    exit 1
fi

build_dir="$(mktemp -d "${TMPDIR:-/tmp}/ashywall-package.XXXXXXXX")"
trap 'rm -rf -- "$build_dir"' EXIT

git archive --format=tar --prefix=ashywall/ HEAD > "$build_dir/ashywall-source.tar"
cp -- pkgbuild/PKGBUILD "$build_dir/PKGBUILD"
source_checksum="$(sha256sum -- "$build_dir/ashywall-source.tar")"
source_checksum="${source_checksum%% *}"
cat >> "$build_dir/PKGBUILD" <<LOCAL_SOURCE

# Local Git snapshot: replace the upstream VCS source and verify its archive.
source=('ashywall-source.tar')
md5sums=()
sha256sums=('$source_checksum')
LOCAL_SOURCE

mkdir -p -- "$PROJECT_ROOT/dist/packages"
export PKGDEST="$PROJECT_ROOT/dist/packages"
export SRCDEST="$build_dir"
export SRCPKGDEST="$build_dir"
export BUILDDIR="$build_dir/build"
export LOGDEST="$build_dir"

printf 'Building AshyWall from local commit %s\n' "$(git rev-parse --short HEAD)"
cd "$build_dir"
makepkg --force --cleanbuild --clean "$@"
