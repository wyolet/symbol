import re

from ..blob import Blob
from ..language import Language
from .base import BaseStrategy


class XMLStrategy(BaseStrategy):
    SEARCH_SCOPE = 2

    def call(self, blob: Blob, candidates: list):
        if candidates:
            return candidates

        header = "\n".join(blob.first_lines(self.SEARCH_SCOPE))

        if re.search(r"<\?xml\s+version=", header, re.IGNORECASE):
            xml_language = Language.find_by_name("XML")
            return [xml_language] if xml_language else []

        return []
