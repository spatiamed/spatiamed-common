"""Every package directory must be listed in [tool.setuptools] packages.

The list is explicit. A package missing from it installs from a git tag
WITHOUT that subpackage while an editable overlay still works, so the
failure would first appear in a consumer's CI after the tag is cut.
"""

import tomllib
from pathlib import Path

ROOT = Path(__file__).parent.parent


def test_every_package_dir_is_declared():
    declared = set(
        tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["setuptools"]["packages"]
    )
    on_disk = {
        ".".join(p.parent.relative_to(ROOT).parts)
        for p in (ROOT / "sm_common").rglob("__init__.py")
    }
    assert on_disk <= declared, f"undeclared packages: {sorted(on_disk - declared)}"
