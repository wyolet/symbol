"""The root --format option applies to the subcommand or is rejected."""

import json
import re
from pathlib import Path

from typer.testing import CliRunner

from wyolet.symbol.cli import app

runner = CliRunner()


def _project(tmp_path: Path) -> Path:
    (tmp_path / "app.py").write_text("def main():\n    return 1\n")
    return tmp_path


def test_global_format_reaches_subcommand(tmp_path: Path):
    result = runner.invoke(app, ["--format", "json", "loc", str(_project(tmp_path))])
    assert result.exit_code == 0, result.output
    assert "summary" in json.loads(result.output)


def test_subcommand_format_overrides_global(tmp_path: Path):
    result = runner.invoke(app, ["--format", "json", "loc", str(_project(tmp_path)), "--format", "rich"])
    assert result.exit_code == 0, result.output
    assert "symbol loc" in result.output


def test_global_format_rejected_without_subcommand_support(tmp_path: Path):
    result = runner.invoke(app, ["--format", "json", "init", str(_project(tmp_path))])
    assert result.exit_code != 0
    # Click renders usage errors in a boxed, colored panel that wraps at the
    # terminal width, so match on the flattened text.
    text = re.sub(r"[│╭╮╰╯─]", " ", re.sub(r"\x1b\[[0-9;]*m", "", result.output))
    assert "has no --format option" in " ".join(text.split())
