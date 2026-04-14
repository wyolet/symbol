"""Tests for side effect detection."""

from pathlib import Path

import ca_tools.checkers  # noqa: F401
from ca_tools.checkers.side_effects import detect
from ca_tools.shared.context import build_context


def _run(tmp_path: Path, **kwargs):
    ctx = build_context(tmp_path, **kwargs)
    results = []
    for filepath in ctx.cache.files:
        tree = ctx.cache.get_ast(filepath)
        results.extend(detect(ctx, filepath, tree))
    return results


def test_detects_bare_call(tmp_path: Path):
    (tmp_path / "mod.py").write_text("load_dotenv()\n")
    effects = _run(tmp_path)
    assert len(effects) == 1
    assert effects[0].call_text == "load_dotenv()"


def test_detects_dotted_call(tmp_path: Path):
    (tmp_path / "mod.py").write_text("db.connect()\n")
    effects = _run(tmp_path)
    assert len(effects) == 1
    assert effects[0].call_text == "db.connect()"


def test_skips_safe_calls(tmp_path: Path):
    (tmp_path / "mod.py").write_text("""
import logging
logger = logging.getLogger(__name__)
T = TypeVar("T")
app = FastAPI()
router = APIRouter()
""")
    effects = _run(tmp_path)
    assert len(effects) == 0


def test_skips_class_instantiation(tmp_path: Path):
    (tmp_path / "mod.py").write_text("MyClass()\nSomeFactory()\n")
    effects = _run(tmp_path)
    assert len(effects) == 0


def test_skips_private_calls(tmp_path: Path):
    (tmp_path / "mod.py").write_text("_internal_setup()\n")
    effects = _run(tmp_path)
    assert len(effects) == 0


def test_calls_inside_functions_not_flagged(tmp_path: Path):
    (tmp_path / "mod.py").write_text("""
def startup():
    load_dotenv()
    db.connect()
""")
    effects = _run(tmp_path)
    assert len(effects) == 0


def test_exclude_pattern(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "vendor").mkdir()
    (tmp_path / "src" / "app.py").write_text("setup()\n")
    (tmp_path / "vendor" / "lib.py").write_text("init()\n")
    effects = _run(tmp_path, exclude=["vendor/*"])
    assert len(effects) == 1
    assert "app.py" in str(effects[0].filepath)
