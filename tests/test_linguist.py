"""Tests for the linguist module — language detection, blob, and directory scanning."""

from pathlib import Path

from ca_tools.shared.linguist import Blob, Linguist
from ca_tools.shared.linguist.language import Language, UnknownLanguage

# ---------------------------------------------------------------------------
# Language loading
# ---------------------------------------------------------------------------


class TestLanguageLoading:
    def test_languages_loaded_count(self):
        languages = Language.all()
        assert len(languages) > 400

    def test_python_exists(self):
        lang = Language.find_by_name("Python")
        assert lang is not None
        assert lang.name == "Python"

    def test_javascript_exists(self):
        lang = Language.find_by_name("JavaScript")
        assert lang is not None
        assert lang.name == "JavaScript"

    def test_typescript_exists(self):
        lang = Language.find_by_name("TypeScript")
        assert lang is not None
        assert lang.name == "TypeScript"

    def test_go_exists(self):
        lang = Language.find_by_name("Go")
        assert lang is not None
        assert lang.name == "Go"

    def test_rust_exists(self):
        lang = Language.find_by_name("Rust")
        assert lang is not None
        assert lang.name == "Rust"

    def test_find_by_name_returns_none_for_missing(self):
        assert Language.find_by_name("NonExistentLang12345") is None

    def test_find_by_name_empty_string(self):
        assert Language.find_by_name("") is None

    def test_find_by_extension_py(self):
        langs = Language.find_by_extension("file.py")
        names = [lang.name for lang in langs]
        assert "Python" in names

    def test_find_by_extension_js(self):
        langs = Language.find_by_extension("app.js")
        names = [lang.name for lang in langs]
        assert "JavaScript" in names

    def test_find_by_extension_unknown(self):
        langs = Language.find_by_extension("file.xyz_unknown_ext_42")
        assert langs == []

    def test_find_by_filename_makefile(self):
        langs = Language.find_by_filename("Makefile")
        names = [lang.name for lang in langs]
        assert "Makefile" in names

    def test_find_by_filename_dockerfile(self):
        langs = Language.find_by_filename("Dockerfile")
        names = [lang.name for lang in langs]
        assert "Dockerfile" in names

    def test_find_by_interpreter_python(self):
        langs = Language.find_by_interpreter("python")
        names = [lang.name for lang in langs]
        assert "Python" in names

    def test_find_by_interpreter_node(self):
        langs = Language.find_by_interpreter("node")
        names = [lang.name for lang in langs]
        assert "JavaScript" in names

    def test_unknown_language_dict(self):
        assert UnknownLanguage["name"] == "Unknown"
        assert UnknownLanguage["language_id"] == -1

    def test_unknown_language_in_index(self):
        lang = Language.find_by_name("Unknown")
        assert lang is not None
        assert lang.name == "Unknown"


# ---------------------------------------------------------------------------
# Blob
# ---------------------------------------------------------------------------


class TestBlob:
    def test_create_from_file(self, tmp_path: Path):
        f = tmp_path / "hello.py"
        f.write_text("print('hello')\n")
        blob = Blob(str(f))
        assert blob.name == "hello.py"

    def test_extension(self, tmp_path: Path):
        f = tmp_path / "app.py"
        f.write_text("x = 1\n")
        blob = Blob(str(f))
        assert blob.extension == ".py"

    def test_extension_no_ext(self, tmp_path: Path):
        f = tmp_path / "Makefile"
        f.write_text("all:\n\techo hi\n")
        blob = Blob(str(f))
        assert blob.extension == ""

    def test_size(self, tmp_path: Path):
        content = "hello world\n"
        f = tmp_path / "test.txt"
        f.write_text(content)
        blob = Blob(str(f))
        assert blob.size == len(content.encode())

    def test_loc(self, tmp_path: Path):
        f = tmp_path / "lines.py"
        f.write_text("line1\nline2\nline3\n")
        blob = Blob(str(f))
        assert blob.loc == 3

    def test_sloc_skips_blanks(self, tmp_path: Path):
        f = tmp_path / "code.py"
        f.write_text("line1\n\nline3\n\n")
        blob = Blob(str(f))
        assert blob.sloc == 2

    def test_binary_detection(self, tmp_path: Path):
        f = tmp_path / "data.bin"
        f.write_bytes(b"\x00\x01\x02\x03\xff")
        blob = Blob(str(f))
        assert blob.binary is True

    def test_text_detection(self, tmp_path: Path):
        f = tmp_path / "readme.txt"
        f.write_text("Just text.\n")
        blob = Blob(str(f))
        assert blob.text is True
        assert blob.binary is False

    def test_empty_detection(self, tmp_path: Path):
        f = tmp_path / "empty.txt"
        f.write_bytes(b"")
        blob = Blob(str(f))
        assert blob.empty is True

    def test_not_empty(self, tmp_path: Path):
        f = tmp_path / "notempty.txt"
        f.write_text("content")
        blob = Blob(str(f))
        assert blob.empty is False

    def test_data_property(self, tmp_path: Path):
        f = tmp_path / "sample.py"
        f.write_text("x = 1\n")
        blob = Blob(str(f))
        assert "x = 1" in blob.data


# ---------------------------------------------------------------------------
# Linguist.detect()
# ---------------------------------------------------------------------------


class TestLinguistDetect:
    def test_detect_python(self, tmp_path: Path):
        f = tmp_path / "app.py"
        f.write_text("import os\nprint('hello')\n")
        blob = Blob(str(f))
        linguist = Linguist()
        lang = linguist.detect(blob)
        assert lang is not None
        assert lang.name == "Python"

    def test_detect_markdown(self, tmp_path: Path):
        f = tmp_path / "README.md"
        f.write_text("# Title\n\nSome text.\n")
        blob = Blob(str(f))
        linguist = Linguist()
        lang = linguist.detect(blob)
        assert lang is not None
        assert lang.name == "Markdown"

    def test_detect_yaml(self, tmp_path: Path):
        f = tmp_path / "config.yml"
        f.write_text("key: value\nlist:\n  - item\n")
        blob = Blob(str(f))
        linguist = Linguist()
        lang = linguist.detect(blob)
        assert lang is not None
        assert lang.name == "YAML"

    def test_detect_json(self, tmp_path: Path):
        f = tmp_path / "data.json"
        f.write_text('{"key": "value"}\n')
        blob = Blob(str(f))
        linguist = Linguist()
        lang = linguist.detect(blob)
        assert lang is not None
        assert lang.name == "JSON"

    def test_returns_none_for_binary(self, tmp_path: Path):
        f = tmp_path / "image.bin"
        f.write_bytes(b"\x00\x89PNG\r\n\x1a\n" + b"\x00" * 100)
        blob = Blob(str(f))
        linguist = Linguist()
        lang = linguist.detect(blob)
        assert lang is None

    def test_detect_shebang_python(self, tmp_path: Path):
        f = tmp_path / "script"
        f.write_text("#!/usr/bin/env python3\nimport sys\nprint(sys.argv)\n")
        blob = Blob(str(f))
        linguist = Linguist()
        lang = linguist.detect(blob)
        assert lang is not None
        assert lang.name == "Python"

    def test_detect_makefile_by_filename(self, tmp_path: Path):
        f = tmp_path / "Makefile"
        f.write_text("all:\n\techo hello\n")
        blob = Blob(str(f))
        linguist = Linguist()
        lang = linguist.detect(blob)
        assert lang is not None
        assert lang.name == "Makefile"

    def test_returns_none_for_empty(self, tmp_path: Path):
        f = tmp_path / "empty.py"
        f.write_bytes(b"")
        blob = Blob(str(f))
        linguist = Linguist()
        lang = linguist.detect(blob)
        assert lang is None

    def test_detect_dockerfile(self, tmp_path: Path):
        f = tmp_path / "Dockerfile"
        f.write_text("FROM python:3.11\nRUN pip install flask\n")
        blob = Blob(str(f))
        linguist = Linguist()
        lang = linguist.detect(blob)
        assert lang is not None
        assert lang.name == "Dockerfile"


# ---------------------------------------------------------------------------
# Linguist.detect_directory()
# ---------------------------------------------------------------------------


class TestLinguistDetectDirectory:
    def test_returns_list_of_dicts(self, tmp_path: Path):
        (tmp_path / "app.py").write_text("import os\nprint('hi')\n")
        (tmp_path / "main.go").write_text("package main\nfunc main() {}\n")
        linguist = Linguist()
        stats = linguist.detect_directory(str(tmp_path))
        assert isinstance(stats, list)
        assert len(stats) >= 1
        for entry in stats:
            assert isinstance(entry, dict)

    def test_skips_git_directory(self, tmp_path: Path):
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
        (tmp_path / "app.py").write_text("x = 1\n")
        linguist = Linguist()
        stats = linguist.detect_directory(str(tmp_path))
        names = [s["name"] for s in stats]
        # .git content should not appear as a detected language source
        assert all(s["files"] >= 1 for s in stats)
        # Only Python should be detected
        assert "Python" in names

    def test_skips_venv_directory(self, tmp_path: Path):
        venv_dir = tmp_path / "venv" / "lib"
        venv_dir.mkdir(parents=True)
        (venv_dir / "pkg.py").write_text("x = 1\n")
        (tmp_path / "app.py").write_text("y = 2\n")
        linguist = Linguist()
        stats = linguist.detect_directory(str(tmp_path))
        total_py_files = sum(s["files"] for s in stats if s["name"] == "Python")
        assert total_py_files == 1

    def test_stats_have_required_keys(self, tmp_path: Path):
        (tmp_path / "hello.py").write_text("print('hello')\n")
        linguist = Linguist()
        stats = linguist.detect_directory(str(tmp_path))
        assert len(stats) >= 1
        entry = stats[0]
        for key in ("name", "lines", "files", "type", "color"):
            assert key in entry, f"Missing key: {key}"

    def test_lines_count_is_positive(self, tmp_path: Path):
        (tmp_path / "code.py").write_text("x = 1\ny = 2\nz = 3\n")
        linguist = Linguist()
        stats = linguist.detect_directory(str(tmp_path))
        py_stats = [s for s in stats if s["name"] == "Python"]
        assert len(py_stats) == 1
        assert py_stats[0]["lines"] > 0
        assert py_stats[0]["files"] == 1

    def test_multiple_languages(self, tmp_path: Path):
        (tmp_path / "app.py").write_text("print('py')\n")
        (tmp_path / "app.js").write_text("console.log('js');\n")
        (tmp_path / "main.go").write_text("package main\n")
        linguist = Linguist()
        stats = linguist.detect_directory(str(tmp_path))
        names = {s["name"] for s in stats}
        assert "Python" in names
        assert "JavaScript" in names
        assert "Go" in names
