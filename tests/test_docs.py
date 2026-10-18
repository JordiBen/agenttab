"""Docs stay honest: every test the threat model cites must exist, and the judge doc's links must resolve."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _all_test_names() -> set[str]:
    names = set()
    for f in (ROOT / "tests").glob("test_*.py"):
        names |= set(re.findall(r"^(?:async )?def (test_\w+)\(", f.read_text(encoding="utf-8"), re.M))
    return names


def test_threat_model_cites_only_existing_tests():
    cited = set(re.findall(r"`(test_\w+)`", (ROOT / "docs" / "threat-model.md").read_text(encoding="utf-8")))
    assert len(cited) >= 40
    missing = cited - _all_test_names()
    assert not missing, f"threat-model.md cites tests that don't exist: {sorted(missing)}"


def test_docs_relative_links_resolve():
    for doc in ("docs/judge-instructions.md", "docs/threat-model.md", "README.md"):
        p = ROOT / doc
        for target in re.findall(r"\]\(((?!https?://|#)[^)\s]+)\)", p.read_text(encoding="utf-8")):
            assert (p.parent / target.split("#")[0]).exists(), f"{doc}: broken link {target}"
