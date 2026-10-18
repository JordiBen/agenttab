"""Windows reads text files as cp1252 unless told otherwise, and the page, the copy and the eval files hold UTF-8 (·, →, ‘’).
Every text read or write in the repo's Python names its encoding; this scan keeps it that way."""
import ast
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEXT_IO = {"read_text", "write_text"}


def _mode(node):
    m = node.args[1] if len(node.args) > 1 else next((k.value for k in node.keywords if k.arg == "mode"), None)
    return m.value if isinstance(m, ast.Constant) else None


def unencoded_calls(src: str) -> list[int]:
    """Line numbers of read_text()/write_text()/open()/Path.open() calls on text that don't pass encoding=."""
    out = []
    for n in ast.walk(ast.parse(src)):
        if not isinstance(n, ast.Call) or any(k.arg == "encoding" for k in n.keywords):
            continue
        f = n.func
        if isinstance(f, ast.Attribute) and f.attr in TEXT_IO:
            out.append(n.lineno)
        elif (isinstance(f, ast.Name) and f.id == "open") or (isinstance(f, ast.Attribute) and f.attr == "open"):
            if "b" not in str(_mode(n) or ""):
                out.append(n.lineno)
    return sorted(out)


def test_the_scan_catches_each_unencoded_form_and_passes_encoded_or_binary_ones():
    bad = 'p.read_text()\np.write_text(s)\nopen("a")\nopen("a", "w")\nPath("a").open()\nopen("a", mode="r")\n'
    assert unencoded_calls(bad) == [1, 2, 3, 4, 5, 6]
    good = ('p.read_text(encoding="utf-8")\np.write_text(s, encoding="utf-8")\nopen("a", encoding="utf-8")\n'
            'open("a", "rb")\nopen("a", mode="wb")\np.read_bytes()\n')
    assert unencoded_calls(good) == []


def test_every_text_read_and_write_in_the_repo_names_its_encoding():
    files = subprocess.run(["git", "ls-files", "*.py"], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", check=True).stdout.split()
    assert len(files) > 30                                       # the scan really ran over the repo
    found = {f: unencoded_calls((ROOT / f).read_text(encoding="utf-8")) for f in files}
    assert {f: ls for f, ls in found.items() if ls} == {}
