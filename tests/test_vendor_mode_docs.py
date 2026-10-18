"""README and the video script describe vendor mode and where the money goes (both say the flag is on on the live site), and every claim names a test that backs it (rule: docs say only what code and
tests prove; the shop vendors' delivery is simulated, so no vendor action is claimed)."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "tests").glob("test_*.py"))
SIMULATED_CLAIMS = r"vendor (ships|shipped|sends|sent|delivers|delivered)|vendor receives|the vendor gets paid"


def _section(text: str, start: str) -> str:
    i = text.index(start)
    m = re.search(r"\n(#{2,3} |\*\*On screen|\| Claim)", text[i + len(start):])
    return text[i: i + len(start) + (m.start() if m else len(text))]


def _backed(sec: str):
    names = re.findall(r"`(?:tests/[\w/]+\.py::)?(test_\w+)`", sec)
    assert names, "no backing tests named"
    for n in names:
        assert re.search(rf"def {n}\(", TESTS), f"{n} doesn't exist"


def test_readme_where_the_money_goes():
    sec = _section((ROOT / "README.md").read_text(encoding="utf-8"), "### Where the money goes")
    # The flag is on on the live site since 2026-10-18 (reviewer-approved): the README now says so, and tab mode stays
    # described for tab-funded agents. The live-flow claims themselves are checked in tests/test_paypal_flow_docs.py.
    assert "AgentTab's own sandbox merchant account" in sec                    # tab mode, still true for tab agents
    assert "VENDOR_PAYOUTS=1" in sec and "On the live site" in sec and "off on the live site" not in sec
    assert re.search(r"[Ww]ith vendor payouts switched off", sec)
    assert "simulated" in sec and not re.search(SIMULATED_CLAIMS, sec, re.I)
    _backed(sec)


def test_video_script_where_the_money_goes():
    doc = (ROOT / "docs" / "video-script.md").read_text(encoding="utf-8")
    sec = _section(doc, "**Where the money goes")
    # The video is filmed with vendor payouts on, as on the live site (reviewer, 2026-10-19); tab mode keeps one sentence.
    assert "Agents created in the owner console use a prepaid tab with authorize-then-capture holds." in sec
    assert "VENDOR_PAYOUTS=1" in sec and "as on the live site" in sec and "off on the live site" not in sec
    assert "`intent: CAPTURE`" in sec and "`payee`" in sec
    assert "has no `payee` field" not in sec                                    # no longer true of app/ on this branch
    assert not re.search(SIMULATED_CLAIMS.replace("|the vendor gets paid", ""), sec, re.I)
    _backed(sec)
