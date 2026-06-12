"""Verify basic project setup."""

import re
import tomllib
from pathlib import Path


def test_version_matches_pyproject():
    # release.sh bumps pyproject.toml and sm_common/__init__.py together;
    # compare them instead of hardcoding so releases can't break this test.
    from sm_common import __version__

    pyproject = Path(__file__).parent.parent / "pyproject.toml"
    with pyproject.open("rb") as f:
        declared = tomllib.load(f)["project"]["version"]

    assert __version__ == declared
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__)
