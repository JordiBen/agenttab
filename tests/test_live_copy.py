"""Copy that describes what the live site runs (VENDOR_PAYOUTS=1: the demo agent is vendor-funded) says so; tab mode is
only described with a tab qualifier. The landing sentences are the reviewer's final wording. Each claim names a test that
exists and backs it."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML = (ROOT / "static" / "dashboard.html").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")
JUDGE = (ROOT / "docs" / "judge-instructions.md").read_text(encoding="utf-8")
VIDEO = (ROOT / "docs" / "video-script.md").read_text(encoding="utf-8")
FLOW = (ROOT / "tests" / "test_vendor_payouts_flow.py").read_text(encoding="utf-8")
TESTS = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "tests").glob("test_*.py"))
HERO = ("AgentTab lets a business's AI purchasing agent pay vendors through PayPal, inside the budgets and limits you set. "
        "Every purchase is checked by rules and by Claude, and anything big waits for your OK.")
APPROVE = ("Anything over $60 waits for your OK. PayPal pays the vendor only after delivery; if delivery fails, nothing "
           "is charged.")
TAB = "Agents created in the owner console use a prepaid tab with authorize-then-capture holds."


def _test_body(name: str, src: str = FLOW) -> str:
    i = src.index(f"def {name}(")
    m = re.search(r"\n(?:def |@pytest|# -)", src[i + 4:])
    return src[i: i + 4 + (m.start() if m else len(src))]


def _exists(name: str) -> bool:
    return bool(re.search(rf"def {name}\(", TESTS))


# (1) hero
def test_hero_sentence():
    assert f'<p class="lead reveal">{HERO}</p>' in HTML
    assert "PayPal-backed tab" not in HTML and "OK in PayPal" not in HTML


# (2) "You approve big buys" + the test that backs it
def test_approval_caption_and_its_backing_test():
    assert f'<span class="cap-k">You approve big buys.</span><p>{APPROVE}</p>' in HTML
    assert "becomes a PayPal hold" not in HTML and "hold is released" not in HTML
    body = _test_body("test_fulfil_fails_no_paypal_call")     # delivery fails: no PayPal call, nothing charged
    assert "assert pays(pp) == []" in body and '"not charged"' in body and "counted(L, a[\"agent_id\"]) == 0" in body
    body = _test_body("test_paypal_only_after_fulfil_and_amount_is_the_reservation")   # PayPal only after delivery
    assert "fulfil" in body and "pays(pp)" in body


# (3) README public demo
def test_readme_demo_asks_the_vendor_for_a_refund():
    line = next(l for l in README.splitlines() if l.startswith("- **Start demo session**"))
    assert "asks the vendor for a refund" in line and "refunds individual purchases" not in line
    row = next(l for l in README.splitlines() if l.startswith("| POST | `/api/demo/session`"))
    assert "funded with $100" not in row


# (4) judge instructions
def _rows(doc):
    return [l for l in doc.splitlines() if l.startswith("| ") and not l.startswith("|---")]


def test_judge_table_has_the_vendor_payout_row_before_authorize():
    rows = _rows(JUDGE)
    i = next(n for n, r in enumerate(rows) if r.startswith("| Authorize → capture / void |"))
    assert rows[i - 1].startswith("| Vendor payout (capture to the vendor's account) | **Real, sandbox, on the live site** |")
    assert rows[i].startswith("| Authorize → capture / void | **Real, sandbox; tab-funded agents (owner console)** |")


def test_judge_files_list_names_the_vendor_code_that_exists():
    assert "[`app/vendorpay.py`](../app/vendorpay.py)" in JUDGE
    main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    vp = (ROOT / "app" / "vendorpay.py").read_text(encoding="utf-8")
    line = next(l for l in JUDGE.splitlines() if "app/vendorpay.py" in l)
    for fn in re.findall(r"`(\w+)\(\)`", line):
        assert re.search(rf"def {fn}\(", vp) or re.search(rf"def {fn}\(", main), fn
    assert "`vendor_buy()`" in line and "`settle_vendor_payment()`" in line


def test_judge_path_describes_the_vendor_funded_demo():
    path = JUDGE[JUDGE.index("## The 1-minute path"):JUDGE.index("## If you have five more minutes")]
    assert "funded with **$100 through a real PayPal sandbox capture**" not in path and "bought from the tab" not in path
    assert "30-day budget" in path and "Request refund" in path and "Approve (sandbox card)" not in path


# (5) video script
def _part(start: str, end: str) -> str:
    return VIDEO[VIDEO.index(start):VIDEO.index(end)]


def test_video_2c_2d_waits_in_agenttab_then_one_capture_to_the_vendor():
    part = _part("### 2c.", "### 2e.")
    assert not re.search(r"\bholds?\b", part, re.I)
    for s in ("no PayPal call", "intent CAPTURE", "payee", "ShopSuite", "vendor's account", "nobody touches PayPal",
              "freezes"):
        assert s in part, s
    for t in ("test_above_threshold_reserves_no_paypal", "test_approve_then_fulfil_then_one_capture_order",
              "test_one_capture_order_straight_to_the_vendor", "test_fulfil_fails_no_paypal_call",
              "test_still_unknown_after_one_retry"):
        assert t in part and _exists(t), t
    for t in re.findall(r"`(?:tests/[\w/]+\.py::)?(test_\w+)`", part):
        assert _exists(t), t


def test_video_section_4_is_the_vendor_flow_with_one_tab_sentence():
    part = _part("## 4. PayPal", "## 5. End card")
    assert TAB in part and len(re.findall(r"\bholds?\b", part)) == 1      # only the tab sentence mentions holds
    for s in ("intent CAPTURE", "payee", "after delivery", "freezes"):
        assert s in part, s
    assert "off on the live site" not in VIDEO and "behind a PayPal hold" not in VIDEO
    for t in re.findall(r"`(?:tests/[\w/]+\.py::)?(test_\w+)`", part):
        assert _exists(t), t


def _secs(t: str) -> int:
    m, s = t.split(":")
    return int(m) * 60 + int(s)


def test_video_stays_under_three_minutes():
    """The timeline adds up to at most 3:00 and each section's voiceover fits its slot at about 160 words a minute."""
    rows = re.findall(r"^\| \d \| [^|]+ \| (\d:\d\d)–(\d:\d\d) \| (\d+) s \|$", VIDEO, re.M)
    assert len(rows) == 5 and sum(int(r[2]) for r in rows) <= 180 and _secs(rows[-1][1]) <= 180
    for sec in re.split(r"\n(?=#{2,3} \d)", VIDEO):
        m = re.match(r"#{2,3} [^\n]*\((\d:\d\d)–(\d:\d\d)\)", sec)
        words = len(" ".join(re.findall(r"^> (.*)$", sec, re.M)).replace('"', "").split())
        if m and words:
            assert words <= 2.7 * (_secs(m.group(2)) - _secs(m.group(1))), (sec.splitlines()[0], words)


# (6) no live-site description of tab mechanics without the tab qualifier
PHRASES = ("PayPal hold", "in PayPal", "hold is released", "funded with $100")
# Places that describe tab mechanics on purpose (the status only exists for tab holds, a plan written before the flag
# was on, the threat-model heading over the tab-hold tests).
ALLOWED = {"static/app.js": "failed_voided", "docs/vendor-payouts-impl-plan.md": "", "docs/vendor-payouts-plan.md": "",
           "docs/threat-model.md": "## PayPal holds, declines, reconciliation and saved cards"}


def test_no_unqualified_tab_wording_in_static_and_docs():
    hits = []
    for p in [*ROOT.glob("static/*.html"), *ROOT.glob("static/*.js"), *ROOT.glob("docs/*.md")]:
        rel = p.relative_to(ROOT).as_posix()
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if not any(ph in re.sub(r"</?b>|\*\*", "", line) for ph in PHRASES):
                continue
            if rel in ALLOWED and ALLOWED[rel] in line:
                continue
            if re.search(r"\btab\b|tab-funded|not in PayPal", line, re.I):
                continue
            hits.append(f"{rel}:{n}: {line.strip()[:120]}")
    assert hits == []
