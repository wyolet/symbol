"""Tests for side effect detection."""

from pathlib import Path

from ca_tools.audit.sideeffects import detect_sideeffects
from ca_tools.shared.spec import load_spec

SPEC = load_spec()


def test_detects_bare_call(tmp_path: Path):
    f = tmp_path / "mod.py"
    f.write_text("load_dotenv()\n")
    effects = detect_sideeffects(tmp_path, SPEC)
    assert len(effects) == 1
    assert effects[0].call_text == "load_dotenv()"


def test_detects_dotted_call(tmp_path: Path):
    f = tmp_path / "mod.py"
    f.write_text("db.connect()\n")
    effects = detect_sideeffects(tmp_path, SPEC)
    assert len(effects) == 1
    assert effects[0].call_text == "db.connect()"


def test_skips_safe_calls(tmp_path: Path):
    f = tmp_path / "mod.py"
    f.write_text("""
import logging
logger = logging.getLogger(__name__)
T = TypeVar("T")
app = FastAPI()
router = APIRouter()
""")
    effects = detect_sideeffects(tmp_path, SPEC)
    assert len(effects) == 0


def test_skips_class_instantiation(tmp_path: Path):
    f = tmp_path / "mod.py"
    f.write_text("MyClass()\nSomeFactory()\n")
    effects = detect_sideeffects(tmp_path, SPEC)
    assert len(effects) == 0


def test_skips_private_calls(tmp_path: Path):
    f = tmp_path / "mod.py"
    f.write_text("_internal_setup()\n")
    effects = detect_sideeffects(tmp_path, SPEC)
    assert len(effects) == 0


def test_calls_inside_functions_not_flagged(tmp_path: Path):
    f = tmp_path / "mod.py"
    f.write_text("""
def startup():
    load_dotenv()
    db.connect()
""")
    effects = detect_sideeffects(tmp_path, SPEC)
    assert len(effects) == 0


def test_exclude_pattern(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "vendor").mkdir()
    (tmp_path / "src" / "app.py").write_text("setup()\n")
    (tmp_path / "vendor" / "lib.py").write_text("init()\n")
    effects = detect_sideeffects(tmp_path, SPEC, exclude=["vendor/*"])
    assert len(effects) == 1
    assert "app.py" in str(effects[0].filepath)
