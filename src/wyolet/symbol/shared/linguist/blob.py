import mimetypes
import os
import re

import chardet

from .config.load import load_config, load_vendor_regex


class Blob:
    MEGABYTE = 1024 * 1024
    # Hard cap on how much of a file we ever pull into memory. Language
    # detection only needs the head (shebang, modeline, content heuristics);
    # LOC counting of files larger than this is intentionally skipped rather
    # than slurping the whole thing. Without this cap a single multi-GB file
    # (a data dump, a model blob, a vendored bundle) is read IN FULL per Blob,
    # and chardet then allocates several× that buffer — the OOM that can take
    # down the whole machine when several servers index the same tree at once.
    MAX_READ_BYTES = 5 * 1024 * 1024
    # chardet builds analyzers across the entire buffer; bound its input hard.
    ENCODING_SNIFF_BYTES = 64 * 1024

    def __init__(self, path, content=None, symlink=False):
        self.path = path
        self._content = content
        self._symlink = symlink
        self._lines = None
        self._size = None
        self.truncated = False

        if self._content is None:
            # True size comes from a cheap stat — never from len(content), which
            # is now capped. Callers relying on .size (large, LOC bytes) stay
            # correct even when we only read the head.
            try:
                self._size = os.path.getsize(path)
            except OSError:
                self._size = 0
            with open(self.path, "rb") as f:
                self._content = f.read(self.MAX_READ_BYTES)
            self.truncated = self._size > len(self._content)
        elif isinstance(self._content, str):
            self._size = len(self._content.encode("utf-8", errors="ignore"))
        else:
            self._size = len(self._content)

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
        if self._size is not None:
            return self._size
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
        sample = self._content[: self.ENCODING_SNIFF_BYTES]
        detection = chardet.detect(sample)
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
        return bool(load_vendor_regex().search(self.path))

    def documentation(self):
        doc_patterns = load_config("documentation")
        return any(re.search(pattern, self.path) for pattern in doc_patterns)

    def include_in_language_stats(self):
        return not (self.vendored() or self.documentation() or self.generated()) and self.text

    def generated(self):
        return False
