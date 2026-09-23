"""What has to stay true of the package itself."""
from __future__ import annotations

import re
from pathlib import Path

from disensor_cursor import __version__

ROOT = Path(__file__).resolve().parents[1]


def test_the_version_is_the_same_in_both_places():
    """The hooks carry `--generated-by` with the package version, and doctor
    compares it. Two numbers that drift would make that check lie."""
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    declared = re.search(r'^version = "([^"]+)"', pyproject, re.MULTILINE)
    assert declared and declared.group(1) == __version__


def test_the_family_table_travels_in_the_wheel():
    """Read with importlib.resources at runtime: missing from package-data it
    works from a checkout and fails installed, where every model would look
    unmapped and nothing would say why."""
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"families.json"' in pyproject.split("[tool.setuptools.package-data]", 1)[1]


def test_no_em_dashes_in_the_documents():
    for path in [ROOT / "README.md"]:
        assert "—" not in path.read_text(encoding="utf-8"), f"guion largo en {path.name}"
