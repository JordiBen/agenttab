"""Vendored assets are pinned with SRI hashes, so their bytes must survive any checkout. On Windows with
core.autocrlf=true git used to rewrite swagger-ui-bundle.js and swagger-ui.css to CRLF: the hash no longer matched
the integrity attribute, the browser refused the script and /docs showed only its header. .gitattributes now keeps
static/vendor byte for byte and every text file LF."""
import base64
import hashlib
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
GIT = shutil.which("git")
PAGES = ("static/docs.html", "static/dashboard.html")


def _sri(data: bytes) -> str:
    return "sha384-" + base64.b64encode(hashlib.sha384(data).digest()).decode()


def _pinned():
    """(path under the repo, integrity) for every local /static/ asset that a page pins with SRI."""
    out = []
    for page in PAGES:
        html = (ROOT / page).read_text(encoding="utf-8")
        for tag in re.findall(r"<(?:script|link)\b[^>]*>", html):
            ref = re.search(r'\b(?:src|href)="(/static/[^"]+)"', tag)
            sri = re.search(r'\bintegrity="(sha384-[^"]+)"', tag)
            if ref and sri:
                out.append((ref.group(1).lstrip("/"), sri.group(1)))
    return out


def _rules():
    rules = {}
    for line in (ROOT / ".gitattributes").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            pat, *attrs = line.split()
            rules[pat] = attrs
    return rules


def test_pages_pin_the_vendored_assets():
    paths = [p for p, _ in _pinned()]
    assert "static/vendor/swagger-ui-5.33.3/swagger-ui-bundle.js" in paths
    assert "static/vendor/swagger-ui-5.33.3/swagger-ui.css" in paths
    assert all(p.startswith("static/vendor/") for p in paths), paths


def test_gitattributes_keeps_vendored_files_byte_exact_and_text_lf():
    rules = _rules()
    assert "-text" in rules.get("static/vendor/**", []), rules            # never converted, whatever autocrlf says
    assert {"text=auto", "eol=lf"} <= set(rules.get("*", [])), rules        # code checks out LF on every machine
    for ext in ("woff2", "png", "jpg", "ico"):
        assert "binary" in rules.get("*." + ext, []), ext


@pytest.mark.skipif(not GIT, reason="git not installed")
def test_git_itself_reads_the_vendor_rule():
    for path, _ in _pinned():
        r = subprocess.run([GIT, "check-attr", "text", "--", path], cwd=ROOT, capture_output=True, text=True,
                           encoding="utf-8")
        if r.returncode != 0:
            pytest.skip("not a git checkout")
        assert r.stdout.strip().endswith("text: unset"), r.stdout


def test_vendored_bytes_match_their_integrity():
    """The bytes on disk and, in a git checkout, the bytes committed at HEAD both hash to the page's integrity."""
    for path, sri in _pinned():
        assert _sri((ROOT / path).read_bytes()) == sri, path
        if GIT:
            r = subprocess.run([GIT, "show", "HEAD:" + path], cwd=ROOT, capture_output=True)
            if r.returncode == 0:
                assert _sri(r.stdout) == sri, "committed " + path


@pytest.mark.skipif(not GIT, reason="git not installed")
def test_a_windows_style_checkout_with_autocrlf_keeps_the_vendored_bytes(tmp_path):
    """Simulates a judge cloning on Windows: commit the pinned assets with our .gitattributes into a fresh repo,
    then check them out again with core.autocrlf=true. Without the vendor rule git writes CRLF here and SRI fails."""
    src = tmp_path / "src"
    files = [".gitattributes", *PAGES, *[p for p, _ in _pinned()], "static/docs.js", "app/main.py"]
    for f in files:
        (src / f).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / f, src / f)

    def git(*args, cwd=src):
        subprocess.run([GIT, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "core.autocrlf=false",
                        "-c", "init.defaultBranch=main", "-c", "commit.gpgsign=false", *args], cwd=cwd, check=True,
                       capture_output=True)

    git("init", "-q")
    git("add", "-A")
    git("commit", "-q", "-m", "snapshot")
    dst = tmp_path / "win"
    subprocess.run([GIT, "-c", "core.autocrlf=true", "clone", "-q", "-c", "core.autocrlf=true", str(src), str(dst)],
                   check=True, capture_output=True)
    for path, sri in _pinned():
        assert _sri((dst / path).read_bytes()) == sri, path + " changed on an autocrlf checkout"
    for f in ("static/docs.js", "app/main.py", "static/docs.html"):
        assert b"\r\n" not in (dst / f).read_bytes(), f + " checked out with CRLF"
