"""The README and the landing page's PayPal line describe the PayPal flow the live site runs now (VENDOR_PAYOUTS=1:
the demo agent is vendor-funded). Every README sentence in that section links to the code or names a test, and every
code link with a line anchor points at the line that defines what it names (so the anchors can't drift silently)."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")
HTML = (ROOT / "static" / "dashboard.html").read_text(encoding="utf-8")
TESTS = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "tests").glob("test_*.py"))
SIMULATED_CLAIMS = r"vendor (ships|shipped|sends|sent|delivers|delivered)|vendor receives|the vendor gets paid"
MODEL_NAMES = r"claude-|haiku|sonnet|opus|gemini|gpt|anthropic|openai"


def _money_section() -> str:
    i = README.index("### Where the money goes")
    m = re.search(r"\n#{2,3} ", README[i + 5:])
    return README[i: i + 5 + m.start()]


def test_readme_says_what_the_live_site_runs():
    sec = _money_section()
    assert "On the live site" in sec and "`VENDOR_PAYOUTS=1`" in sec
    assert "off on the live site" not in README and "vendor payouts off" not in README
    for s in ("Orders v2", "`intent: CAPTURE`", "`payee`", "Vault", "after the delivery step", "refund request",
              "reconcile", "`payment_unknown`", "frozen", "multiparty", "seller onboarding"):
        assert s in sec, s
    assert "simulated" in sec and not re.search(SIMULATED_CLAIMS, sec, re.I)


def test_every_paragraph_of_the_section_is_backed():
    sec = _money_section()
    bullets = [b for b in re.split(r"\n- ", sec)[1:]]
    assert len(bullets) >= 5
    for b in bullets:
        assert re.search(r"\]\((app|tests|docs)/[^)]+\)|`test_\w+`", b), b[:80]
    for n in re.findall(r"`(?:tests/[\w/]+\.py::)?(test_\w+)`", sec):
        assert re.search(rf"def {n}\(", TESTS), f"{n} doesn't exist"


def test_code_line_anchors_point_at_what_they_name():
    links = re.findall(r"\[`([\w.]+)`\]\(((?:app|tests)/[\w/]+\.py)#L(\d+)\)", README)
    assert len(links) >= 6
    for name, path, line in links:
        lines = (ROOT / path).read_text(encoding="utf-8").splitlines()
        assert name.split(".")[-1] in lines[int(line) - 1], (name, path, line, lines[int(line) - 1])


def test_landing_paypal_line_matches_the_live_flow():
    dev = HTML[HTML.index('<ul class="ticks dev-list">'):HTML.index("</ul>", HTML.index('<ul class="ticks dev-list">'))]
    li = next(i for i in re.findall(r"<li>(.*?)</li>", dev, re.S) if "<b>PayPal:</b>" in i)
    assert 'href="https://github.com/JordiBen/agenttab#where-the-money-goes"' in li
    text = re.sub(r"<[^>]+>", "", li)
    for s in ("Orders v2", "intent CAPTURE", "payee", "Vault", "after delivery", "request to the vendor", "reconcile",
              "frozen", "multiparty"):
        assert s in text, s
    assert "AUTHORIZE" not in text                                   # the demo agent no longer uses holds
    assert not re.search(MODEL_NAMES, text, re.I) and not re.search(r"\bms\b|\d+\s*(calls|tests|cases)", text)
