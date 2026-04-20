import mimetypes
import os
import re

import chardet

from .config.load import load_config


class Blob:
    MEGABYTE = 1024 * 1024

    def __init__(self, path, content=None, symlink=False):
        self.path = path
        self._content = content
        self._symlink = symlink
        self._lines = None

        if self._content is None:
            with open(self.path, "rb") as f:
                self._content = f.read()

    def __repr__(self):
        return f"<Blob {self.path}>"

    @property
    def name(self):
        return os.path.basename(self.path)

    @property
    def data(self):
        if isinstance(self._content, bytes):
            return self._content.decode(errors="ignore")
        return self._content

    @property
    def size(self):
        return len(self._content)

    @property
    def extension(self):
        return self.extensions[-1] if self.extensions else ""

    @property
    def extensions(self):
        segments = self.name.lower().split(".")
        if len(segments) < 2:
            return []
        return ["." + ".".join(segments[i:]) for i in range(1, len(segments))]

    @property
    def symlink(self):
        return self._symlink or os.path.islink(self.path)

    @property
    def extname(self):
        return os.path.splitext(self.name)[1]

    @property
    def _mime_type(self):
        return mimetypes.guess_type(self.name)[0] or "text/plain"

    @property
    def mime_type(self):
        return self._mime_type

    @property
    def binary_mime_type(self):
        return self._mime_type.startswith("application/") or self._mime_type.startswith("image/")

    @property
    def likely_binary(self):
        try:
            self._content[:1024].decode("utf-8")
            return False
        except UnicodeDecodeError:
            return True

    def detect_encoding(self):
        detection = chardet.detect(self._content)
        if detection["confidence"] > 0.5:
            return detection
        return None

    @property
    def encoding(self):
        detected = self.detect_encoding()
        return detected["encoding"] if detected else None

    @property
    def binary(self):
        return self._content and b"\x00" in self._content

    @property
    def empty(self):
        return not self._content

    @property
    def text(self):
        return not self.binary

    @property
    def image(self):
        return self.extname.lower() in [".png", ".jpg", ".jpeg", ".gif"]

    @property
    def large(self):
        return self.size > self.MEGABYTE

    @property
    def viewable(self):
        return not self.large and self.text

    def read_lines(self):
        if self._lines is None:
            self._lines = self.data.splitlines()
        return self._lines

    def first_lines(self, n=10):
        return self.read_lines()[:n]

    def last_lines(self, n=10):
        return self.read_lines()[-n:]

    @property
    def loc(self):
        return len(self.read_lines())

    @property
    def sloc(self):
        return sum(1 for line in self.read_lines() if line.strip())

    def safe_to_colorize(self):
        return not self.large and self.text and not self.high_ratio_of_long_lines()

    def high_ratio_of_long_lines(self):
        if self.loc == 0:
            return False
        return self.size / self.loc > 5000

    def vendored(self):
        vendor_patterns = load_config("vendor")
        return any(re.search(pattern, self.path) for pattern in vendor_patterns)

    def documentation(self):
        doc_patterns = load_config("documentation")
        return any(re.search(pattern, self.path) for pattern in doc_patterns)

    def include_in_language_stats(self):
        return not (self.vendored() or self.documentation() or self.generated()) and self.text

    def generated(self):
        return False
