"""Reviewer t81u (2026-10-19), on fix/hero-polish:
(1) the page title says "a budget, not your card"; the old "a tab, not ..." wording is gone from every file in static/,
    and og:/twitter: title/description (if present) say the same thing as <title>.
(2) the Measured proof line under the hero enters at the same pace as the honest line, via its own CSS animation
    (it is shown only once /api/scorecard answers, so the .reveal observer cannot pace it), and reduced motion turns it off."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
HTML = (STATIC / "dashboard.html").read_text(encoding="utf-8")
CSS = (STATIC / "app.css").read_text(encoding="utf-8")
TITLE = "AgentTab: give your AI agent a budget, not your card"


def test_title_is_exact():
    titles = re.findall(r"<title>(.*?)</title>", HTML, flags=re.S)
    assert titles == [TITLE]


def test_no_a_tab_not_anywhere_in_static():
    hits = []
    for p in sorted(STATIC.rglob("*")):
        if p.is_file() and b"a tab, not" in p.read_bytes().lower():
            hits.append(str(p.relative_to(ROOT)))
    assert hits == []


def test_social_meta_matches_title_if_present():
    for m in re.finditer(r'<meta\s+[^>]*(?:property|name)="((?:og|twitter):(?:title|description))"[^>]*>', HTML):
        content = re.search(r'content="([^"]*)"', m.group(0)).group(1)
        assert "a tab" not in content.lower(), m.group(1)
        if m.group(1).endswith(":title"):
            assert content == TITLE, m.group(1)
        else:
            assert "budget" in content.lower(), m.group(1)


def test_proof_line_is_not_paced_by_the_reveal_observer():
    tag = re.search(r'<a[^>]*id="heroproof"[^>]*>', HTML).group(0)
    assert "reveal" not in re.search(r'class="([^"]*)"', tag).group(1).split()


def test_proof_line_has_its_own_entrance_matching_the_honest_line():
    kf = re.search(r"@keyframes\s+heroproof-in\s*\{(.*?)\}\s*\}", CSS, flags=re.S)
    assert kf and "opacity:0" in kf.group(1).replace(" ", "")
    rule = re.search(r"\.heroproof\{([^}]*)\}", CSS).group(1).replace(" ", "")
    # same duration/curve as .reveal (.24s var(--ease)) and the honest line's stagger delay (4th hero .reveal: 3*60ms)
    assert "animation:heroproof-in.24svar(--ease).18sboth" in rule


def test_reduced_motion_kills_the_proof_line_animation():
    rm = CSS[CSS.index("@media (prefers-reduced-motion: reduce){"):]
    rm = rm[:rm.index("\n}") + 2]
    assert "animation:none !important" in rm
