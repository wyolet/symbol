import re

from ..blob import Blob
from ..language import Language
from .base import BaseStrategy


class ManpageStrategy(BaseStrategy):
    MANPAGE_EXTS = re.compile(r"\.(?:[1-9](?![0-9])[a-z_0-9]*|0p|n|man|mdoc)(?:\.in)?$", re.IGNORECASE)

    def call(self, blob: Blob, candidates: list):
        if candidates:
            return candidates

        if self.MANPAGE_EXTS.search(blob.name):
            roff_manpage = Language.find_by_name("Roff Manpage")
            roff = Language.find_by_name("Roff")
            return [lang for lang in [roff_manpage, roff] if lang is not None]

        return []
