#!/usr/bin/env python3
"""Check catalog completeness against the BigCommunity language targets."""

import argparse
import json
import subprocess
import tempfile
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--require-complete",
        action="store_true",
        help="Fail if any target language has missing or untranslated messages.",
    )
    options = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    locale_dir = root / "locale"
    manifest = json.loads((locale_dir / "targets.json").read_text())
    source = manifest["source_language"]
    targets = manifest["languages"]
    complete = 0
    invalid = False
    missing = []
    with tempfile.TemporaryDirectory(prefix="ashywall-catalogs-") as scratch:
        for language, name in targets.items():
            if language == source:
                complete += 1
                print(f"{language}: source language ({name})")
                continue
            po = locale_dir / f"{language}.po"
            if not po.is_file():
                missing.append(language)
                print(f"{language}: missing ({name})")
                continue
            mo = Path(scratch) / f"{language}.mo"
            compiled = subprocess.run(
                ["msgfmt", "--check", "-o", str(mo), str(po)],
                capture_output=True,
                text=True,
                check=False,
            )
            packaged = root / "usr/share/locale" / language / "LC_MESSAGES/ashywall.mo"
            if compiled.returncode or not packaged.is_file():
                invalid = True
                print(f"{language}: invalid or missing compiled catalog ({name})")
                print(compiled.stderr, end="")
                continue
            if mo.read_bytes() != packaged.read_bytes():
                invalid = True
                print(f"{language}: compiled catalog differs from source ({name})")
                continue
            compared = subprocess.run(
                ["msgcmp", str(po), str(locale_dir / "ashywall.pot")],
                capture_output=True,
                text=True,
                check=False,
            )
            if compared.returncode:
                missing.append(language)
                print(f"{language}: incomplete ({name})")
                if options.require_complete:
                    print(compared.stderr, end="")
                continue
            complete += 1
            print(f"{language}: complete ({name})")
    print(f"Language coverage: {complete}/{len(targets)} (including English source).")
    if missing:
        print("Release translations pending: " + ", ".join(missing))
    return 1 if invalid or (options.require_complete and missing) else 0


if __name__ == "__main__":
    raise SystemExit(main())
