import re

from ..blob import Blob
from ..language import Language
from .base import BaseStrategy


class ShebangStrategy(BaseStrategy):
    def call(self, blob: Blob, candidates: list):
        if blob.symlink:
            return []

        interpreter = self.get_interpreter(blob.data)
        if not interpreter:
            return []

        languages = Language.find_by_interpreter(interpreter)

        return [lang for lang in languages if lang in candidates] if candidates else languages

    @staticmethod
    def get_interpreter(data: str):
        if not data.startswith("#!"):
            return None

        shebang_line = data.splitlines()[0]

        match = re.match(r"^#!\s*(\S+)", shebang_line)
        if not match:
            return None

        path = match.group(1)
        interpreter = path.split("/")[-1]

        if interpreter == "env":
            args = shebang_line.split()[1:]
            interpreter = next(
                (arg for arg in args if not arg.startswith("-") and "=" not in arg),
                None,
            )

        if not interpreter:
            return None

        interpreter = re.sub(r"\.\d+$", "", interpreter)

        if interpreter == "sh" and any(
            re.search(r"exec (\w+)[\s\"']+\$0[\s\"']+\$@", line) for line in data.splitlines()[:5]
        ):
            if not (match := re.search(r"exec (\w+)", data)):
                return None
            interpreter = match.group(1)

        if interpreter == "osascript" and "-l" in shebang_line:
            return None

        return interpreter
