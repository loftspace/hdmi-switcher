"""Guard against version drift between pyproject.toml and the package."""

from __future__ import annotations

import tomllib
from pathlib import Path

import hdmi_follow


def test_package_version_matches_pyproject() -> None:
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    version = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]
    assert hdmi_follow.__version__ == version
