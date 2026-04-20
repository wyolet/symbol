import re

from ..language import Language
from .base import BaseStrategy

EMACS_MODELINE = re.compile(
    r"-\*-(?:[ \t]*(?=[^:;\s]+[ \t]*-\*-)|(?:.*?[ \t;]|(?<=-\*-))[ \t]*mode[ \t]*:[ \t]*)([^:;\s]+)(?=[ \t;]|(?<![-*])-\*-).*?-\*-",
    re.IGNORECASE,
)

VIM_MODELINE = re.compile(
    r"(?:(?:^|[ \t])(?:vi|Vi(?=m))(?:m[<=>]?[0-9]+|m)?|[ \t]ex)(?=:[ \t]*set?[ \t][^\r\n:]+:|:(?![ \t]*set?[ \t]))(?:(?:[ \t]*:[ \t]*|[ \t])\w*(?:[ \t]*=(?:[^\\\s]|\\.)*)?)*[ \t:](?:filetype|ft|syntax)[ \t]*=(\w+)(?=$|\s|:)",
    re.IGNORECASE,
)


class ModeLineStrategy(BaseStrategy):
    SEARCH_SCOPE = 5
    MODELINES = [EMACS_MODELINE, VIM_MODELINE]

    def call(self, blob, candidates=None):
        if blob.symlink:
            return []

        header = "\n".join(blob.first_lines(self.SEARCH_SCOPE))
        if "UseVimball" in header:
            return []

        footer = "\n".join(blob.last_lines(self.SEARCH_SCOPE))
        detected_language = self.modeline(header + footer)

        if detected_language:
            lang = Language.find_by_alias(detected_language)
            return [lang] if lang else []
        return []

    def modeline(self, data):
        for regex in self.MODELINES:
            match = regex.search(data)
            if match:
                return match.group(1)
        return None
