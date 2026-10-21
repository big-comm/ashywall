# BigCommunity language targets

AshyWall's release target is the 29-language set used by
[LangForge](https://github.com/big-comm/LangForge), confirmed in its
[language definitions](https://github.com/big-comm/LangForge/blob/main/usr/share/langforge/core/languages.py)
on 2026-10-21. `targets.json` records the names and gettext locale codes.
This is a release requirement, not a claim that all catalogs already exist.

The set includes English (the source language), Portuguese and Brazilian
Portuguese. It uses `no` for Norwegian and `zh` for Chinese, matching the
reference project. Use POSIX/gettext `pt_BR`, although the LangForge API
list and some older BigCommunity catalogs use the BCP-47 spelling `pt-BR`.

Required languages:

```
bg cs da de el en es et fi fr he hr hu is it ja ko nl no pl
pt_BR pt ro ru sk sv tr uk zh
```

Current state: English source and a complete Brazilian Portuguese catalog.
The other 27 catalogs are pending. Do not create empty catalogs or copy
English messages into foreign catalogs merely to claim coverage.

The BigCommunity
[translation-aware package template](https://github.com/big-comm/pkgbuild-template-translator)
confirms the package layout and date-based distribution version convention;
its own initial catalogs are not a complete language policy. The
[community website](https://communitybig.org/apps.html) links to the
organization but does not enumerate the 29 languages.

## Verification

```sh
# Development: validate existing catalogs and report missing targets.
python scripts/check-translations.py

# Release: also fail for any missing, fuzzy or untranslated target messages.
python scripts/check-translations.py --require-complete
./scripts/validate.sh --release
```

The checks compare each PO with the current template using GNU gettext,
validate its formatting and plural forms, and compare the compiled MO with
the catalog included in the wheel/package. English needs no translated MO.

Use LangForge or a translator to fill catalogs from `ashywall.pot`.
Preserve format fields such as `{provider}`, `{count}` and `{path}`, GNOME
and provider names, and keyboard shortcuts. Apply each language's own
gettext plural rules; do not copy the Brazilian Portuguese rule into all
languages. Refresh and compile with `./scripts/update-translations.sh`.

Automatic translations require linguistic review, especially plural forms,
security messages, wallpaper actions and read/unread state. Check the
native GNOME layout with long labels, CJK text and Hebrew's right-to-left
direction. Record coverage separately from linguistic and visual review.

All shipped catalogs live at `usr/share/locale/<locale>/LC_MESSAGES/ashywall.mo`
and are included by the existing wheel data mapping. No translation API,
API key or network access is needed at application runtime or package build.
