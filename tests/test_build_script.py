"""Tests for the version check in scripts/build.py."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_build_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("build_script", ROOT / "scripts" / "build.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fake_binary(output: str) -> list[str]:
    # Ignores the appended --version argument and prints a fixed string.
    return [sys.executable, "-c", f"print({output!r})"]


def test_check_version_accepts_matching_binary() -> None:
    build = load_build_script()
    build.check_version(fake_binary("1.2.3"), "1.2.3", ROOT)


def test_check_version_rejects_drifted_binary() -> None:
    build = load_build_script()
    with pytest.raises(SystemExit, match="'0.2.0', expected '0.3.0'"):
        build.check_version(fake_binary("0.2.0"), "0.3.0", ROOT)
