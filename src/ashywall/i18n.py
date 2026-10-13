"""Translation setup: bind the gettext domain and export ``_``."""

import gettext
import os
from typing import Callable

DOMAIN = "ashywall"

# ASHYWALL_LOCALE_DIR lets a source checkout use its own compiled catalogs.
locale_dir = os.environ.get("ASHYWALL_LOCALE_DIR", "/usr/share/locale")

gettext.bindtextdomain(DOMAIN, locale_dir)
gettext.textdomain(DOMAIN)

_: Callable[[str], str] = gettext.gettext
ngettext: Callable[[str, str, int], str] = gettext.ngettext
