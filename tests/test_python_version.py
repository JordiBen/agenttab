"""Render's Python comes from the PYTHON_VERSION env var in its dashboard (3.12.7 on the build of 45aece0). .python-version
keeps that in the repo, so Render, pyenv and uv pick the same interpreter. The dev tools a judge installs to run the
suite are pinned exactly too, so a fresh `pip install -r requirements-dev.txt` gets the same pytest and Playwright."""
import re
from pathlib import Path

from tests.test_pinned_deps import pins

ROOT = Path(__file__).resolve().parent.parent


def test_python_version_file_pins_renders_python():
    f = ROOT / ".python-version"
    assert f.is_file(), ".python-version is missing"
    assert f.read_text(encoding="utf-8").strip() == "3.12.7"


def test_dev_requirements_are_pinned_exactly():
    dev = pins(ROOT / "requirements-dev.txt")             # raises on >=, ~=, bare names, ranges
    assert {"pytest", "playwright"} <= set(dev), sorted(dev)
    for name in ("pluggy", "iniconfig", "packaging", "pygments", "greenlet", "pyee"):   # their own dependencies
        assert name in dev, f"{name} is not pinned in requirements-dev.txt"


def test_dev_requirements_do_not_repin_runtime_packages_differently():
    dev, lock = pins(ROOT / "requirements-dev.txt"), pins(ROOT / "constraints.txt")
    for name, ver in dev.items():
        assert name not in lock or lock[name] == ver, f"{name}: dev {ver} vs constraints {lock[name]}"
