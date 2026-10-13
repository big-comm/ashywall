#!/usr/bin/env bash
# Refresh the POT template, merge it into every .po and compile the catalogs.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

mapfile -t sources < <(find src -name '*.py' | sort)
xgettext --language=Python --from-code=UTF-8 \
    --keyword=_ --keyword=ngettext:1,2 \
    --package-name=ashywall --add-comments=Translators \
    -o locale/ashywall.pot "${sources[@]}"

for po in locale/*.po; do
    lang="$(basename "$po" .po)"
    msgmerge --quiet --update --backup=none "$po" locale/ashywall.pot
    install -d "usr/share/locale/$lang/LC_MESSAGES"
    msgfmt --check --statistics -o "usr/share/locale/$lang/LC_MESSAGES/ashywall.mo" "$po"
done
