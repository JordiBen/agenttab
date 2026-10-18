"""A fresh install (a judge's clone, or a new Render build) must get the exact versions the live site was built with.
Loose specifiers let `pip install -r requirements.txt` pull whatever was released that morning (starlette 1.9.0 landed
on 2026-10-18 and changed TestClient). requirements.txt pins every direct dependency with ==, and constraints.txt, which
requirements.txt pulls in with -c, pins every transitive one."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PIN = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[A-Za-z0-9,._-]+\])?==(?P<ver>[0-9][0-9A-Za-z.+!-]*)$")
DIRECT = {"fastapi", "starlette", "uvicorn", "httpx", "pydantic", "beautifulsoup4", "anthropic"}


def _canon(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _lines(path: Path) -> list[str]:
    out = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split(" #", 1)[0].strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def pins(path: Path) -> dict[str, str]:
    """name -> version for every requirement line; raises on anything that is not name==version."""
    got = {}
    for line in _lines(path):
        if line.startswith("-"):
            assert line == "-c constraints.txt", f"{path.name}: unexpected option line {line!r}"
            continue
        m = PIN.match(line)
        assert m, f"{path.name}: {line!r} is not pinned with == (no >=, ~=, <, * or bare names)"
        name = _canon(m["name"])
        assert name not in got, f"{path.name}: {name} listed twice"
        got[name] = m["ver"]
    return got


def test_every_direct_dependency_is_pinned_exactly():
    req = pins(ROOT / "requirements.txt")
    assert DIRECT <= set(req), f"missing direct pins: {sorted(DIRECT - set(req))}"   # starlette pinned explicitly too


def test_requirements_pull_in_the_constraints_file():
    assert "-c constraints.txt" in _lines(ROOT / "requirements.txt")
    assert (ROOT / "constraints.txt").is_file()


def test_constraints_pin_everything_and_agree_with_requirements():
    req, lock = pins(ROOT / "requirements.txt"), pins(ROOT / "constraints.txt")
    assert len(lock) > len(req)                                          # transitive deps are there, not just the direct ones
    for name, ver in req.items():
        assert lock.get(name) == ver, f"{name}: requirements.txt {ver} but constraints.txt {lock.get(name)}"


def test_the_pin_checker_rejects_loose_specifiers(tmp_path):
    for bad in ("fastapi>=0.110", "fastapi~=0.146", "fastapi", "fastapi==0.*", "fastapi<1", "-r other.txt"):
        f = tmp_path / "requirements.txt"
        f.write_text(bad + "\n", encoding="utf-8")
        try:
            pins(f)
        except AssertionError:
            continue
        raise AssertionError(f"{bad!r} was accepted")
    f.write_text("uvicorn[standard]==0.54.0  # comment\n", encoding="utf-8")
    assert pins(f) == {"uvicorn": "0.54.0"}
