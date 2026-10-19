"""Hero polish (reviewer t80u, 2026-10-19): the AI-guard status is a round ink dot (not a dropdown triangle),
the hero receipt is shorter so the copy column and the paper bottom line up, and under the honesty line the page
shows a Measured proof line whose numbers come from the same /api/scorecard source as the Proof section.
docs/video-script.md: 2a is filmed on the hosted site, 2e says "the same guard and limits", and Recording rules
describe the hosted-site Take B (up to 3 takes, 1440 px, blur before each segment; DNS caveat only for local filming)."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CSS = (ROOT / "static" / "app.css").read_text(encoding="utf-8")
HTML = (ROOT / "static" / "dashboard.html").read_text(encoding="utf-8")
JS = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
VIDEO = (ROOT / "docs" / "video-script.md").read_text(encoding="utf-8")
RESULTS = json.loads((ROOT / "eval" / "results.json").read_text(encoding="utf-8"))
HELDOUT = json.loads((ROOT / "eval" / "heldout-results.json").read_text(encoding="utf-8"))


def _sec(start: str, end: str) -> str:
    return VIDEO[VIDEO.index(start):VIDEO.index(end)]


def _css_rule(selector: str) -> str:
    # one-line or brace-block rule whose selectors contain the exact token
    for m in re.finditer(rf"[^{{}}]*{re.escape(selector)}[^{{}}]*\{{([^}}]*)\}}", CSS):
        return m.group(1)
    raise AssertionError(f"no CSS rule for {selector}")


# ---------- (1) AI guard status: round dot, not a dropdown triangle ----------
def test_live_indicator_is_an_8px_round_dot_not_a_triangle():
    rule = _css_rule(".live i")
    assert "clip-path" not in rule
    assert re.search(r"border-radius\s*:\s*50%", rule)
    assert re.search(r"width\s*:\s*8px", rule) and re.search(r"height\s*:\s*8px", rule)
    live = _css_rule(".live")
    assert re.search(r"cursor\s*:\s*default", live)
    # ink when live, stamp-hold when busy, muted when off (background of the <i>)
    assert "background:var(--ink)" in rule or "background: var(--ink)" in rule
    assert "background:var(--stamp-hold)" in CSS[CSS.index(".live.busy"):CSS.index(".live.busy") + 200]
    assert "background:var(--muted)" in CSS[CSS.index(".live.off"):CSS.index(".live.off") + 120]
    assert 'id="navlive"' in HTML and 'role="status"' in HTML          # title attribute stays via setLive()
    assert "el.title = LIVE_TIP[st]" in JS


# ---------- (2) hero receipt height + Measured proof line ----------
def test_hero_receipt_height_is_the_shorter_clamp():
    rule = _css_rule(".receipt")
    assert "clamp(420px,38vw,448px)" in rule.replace(" ", "")  # reviewer asked 520; at 1440 that left gap 228, so max is 448 to meet gap<160
    assert "clamp(520px,56vw,700px)" not in CSS.replace(" ", "")


def test_hero_has_a_measured_proof_line_under_the_honest_line():
    hero = HTML[HTML.index('<header class="hero"'):HTML.index("</header>")]
    assert 'class="honest' in hero
    assert re.search(r'id="heroproof"[^>]*href="#proof"', hero) or re.search(r'href="#proof"[^>]*id="heroproof"', hero)
    assert hero.index('class="honest') < hero.index('id="heroproof"')
    # filled by scorecard() from the same payload as the Proof section; no hardcoded attack/legit counts in HTML
    assert "Measured:" in JS and "attacks stopped" in JS and "legit buys allowed" in JS and "separate test set" in JS
    assert "scorecard()" in JS and "$('heroproof')" in JS
    hard = re.findall(r"Measured:[^`'\"]{0,120}", JS)
    assert hard and not re.search(r"\b\d+\b", hard[0]), hard          # the template itself carries no digits


def test_measured_line_numbers_come_from_the_results_files():
    """The template interpolates the same fields the Proof table uses; the committed files are the source of truth."""
    ai, h = RESULTS["modes"]["ai"], HELDOUT["ai"]["summary"]
    assert "m.ai.attacks_stopped" in JS.replace(" ", "") or "m['ai'].attacks_stopped" in JS or "m.ai?.attacks_stopped" in JS \
        or re.search(r"modes\.?\[.?ai.?\]\.attacks_stopped|m\.ai\.attacks_stopped|ai\.attacks_stopped", JS)
    # whatever the JS reads, the files hold these values, and an e2e check asserts the rendered text
    assert ai["attacks_stopped"] == 47 and ai["legit_correct"] == 20 and h["attacks_stopped"] == 26


# ---------- (3) video script ----------
def test_2a_names_the_hosted_site():
    a2 = _sec("### 2a.", "### 2b.")
    assert "https://agenttab.onrender.com" in a2
    assert "127.0.0.1" not in a2


def test_2e_voiceover_says_the_same_guard_and_limits():
    e2 = _sec("### 2e.", "## 3. Proof")
    vo = " ".join(re.findall(r"^> (.*)$", e2, re.M))
    assert "the same guard and limits" in vo.lower()
    assert "guarded tab" not in vo.lower()


def test_recording_rules_describe_the_hosted_take_b():
    rules = re.sub(r"\s+", " ", _sec("## Recording rules", "## Timeline"))
    assert "https://agenttab.onrender.com" in rules
    assert re.search(r"up to 3 takes|at most 3 takes|max(?:imum)? 3 takes", rules, re.I)
    assert re.search(r"1440\s*px", rules)
    assert re.search(r"\bblur\b|remove focus", rules, re.I)
    # DNS caveat is for whoever films locally, not a hard gate for the hosted take
    assert "socket.gethostbyname('example.com')" in rules
    assert re.search(r"local(ly| filming| instance)", rules, re.I)
    assert "Never click a demo button on the hosted site" not in rules   # Take B is the approved exception
    assert "127.0.0.1:8811" not in rules
