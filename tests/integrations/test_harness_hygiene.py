"""The Bahmni live harness lives in a PUBLIC repo: no default credentials, no AWS ids."""

from __future__ import annotations

import pathlib
import re

HARNESS = pathlib.Path(__file__).resolve().parents[2] / "harness" / "bahmni"


def _sources() -> dict[str, str]:
    assert HARNESS.is_dir(), f"{HARNESS} missing"
    files = sorted(HARNESS.glob("*.py"))
    assert files, "harness/bahmni has no .py files"
    return {f.name: f.read_text() for f in files}


def test_harness_has_no_default_credentials() -> None:
    for name, text in _sources().items():
        for word in ("Admin123", "superman"):
            assert word not in text, f"{name} contains {word!r}"


def test_harness_has_no_aws_ids() -> None:
    patterns = [r"i-0[0-9a-f]{8,}", r"sg-0", r"subnet-0", r"\b\d{12}\b"]
    texts = _sources()
    readme = HARNESS / "README.md"
    if readme.exists():
        texts["README.md"] = readme.read_text()
    for name, text in texts.items():
        for pat in patterns:
            assert not re.search(pat, text), f"{name} matches {pat!r}"
