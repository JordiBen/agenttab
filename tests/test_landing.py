"""feature/landing-redesign: the page follows docs/DESIGN.md (receipt + stamp motif, olive on bone, Roundo) without
changing any number, claim or selector the app and the e2e tests rely on."""
import json
import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

ROOT = Path(__file__).parent.parent
HTML = (ROOT / "static/dashboard.html").read_text()
CSS = (ROOT / "static/app.css").read_text()
JS = (ROOT / "static/app.js").read_text()


def _client(tmp_path):
    s = Settings(paypal_mode="mock", allow_insecure_mock=True, db_path=str(tmp_path / "l.db"),
                 admin_token="adm", public_url="http://testserver", llm_base_url="", llm_api_key="")
    return TestClient(create_app(s, allow_private_targets=True))


def test_roundo_is_self_hosted_with_its_ofl_licence_next_to_it_and_inter_is_gone(tmp_path):
    c = _client(tmp_path)
    r = c.get("/static/fonts/roundo/Roundo-Variable.woff2")
    assert r.status_code == 200 and r.headers["content-type"] == "font/woff2" and r.content[:4] == b"wOF2"
    lic = c.get("/static/fonts/roundo/License/OFL.txt")
    assert lic.status_code == 200 and "SIL Open Font License" in lic.text and "itfoundry/roundo" in lic.text.lower()
    assert "font-family:Roundo" in CSS.replace(" ", "") and "/static/fonts/roundo/Roundo-Variable.woff2" in CSS
    for f in (HTML, CSS, JS):
        assert not re.search(r"\bInter\b", f) and "Nunito" not in f and "monospace" not in f
    assert c.get("/static/fonts/InterVariable.woff2").status_code == 404


def test_scorecard_api_carries_stale_modes_for_the_previous_catalog_label(tmp_path):
    j = _client(tmp_path).get("/api/scorecard").json()
    assert j["stale_modes"] == json.loads((ROOT / "eval/results.json").read_text()).get("stale_modes", [])


def test_design_tokens_and_remove_list():
    for tok in ("--bg: #ecebe7", "--paper: #f4f4f1", "--ink: #38450f", "--stamp-block: #a3261b", "--stamp-hold: #7a5200",
                "--radius: 6px", "--tooth: 12px"):
        assert tok in CSS, tok
    assert "backdrop-filter" not in CSS and "linear-gradient" not in CSS and "radial-gradient" not in CSS
    assert not re.search(r"box-shadow:\s*(?!none)", CSS), "no resting shadows"
    assert "@media (prefers-reduced-motion: reduce)" in CSS
    for gone in ('class="noise"', 'class="aurora"', 'class="gridlines"', 'class="kicker"', '"n">01<', "glass", "grad\""):
        assert gone not in HTML, gone
    assert 'name="color-scheme" content="light"' in HTML


def test_hero_receipt_primary_action_and_honesty_labels():
    """landing-copy v2: the primary action jumps to section 4 (Try it); the live AI status pill lives in the top bar."""
    hero = HTML[HTML.index('<header class="hero"'):HTML.index("</header>")]
    assert re.search(r'class="receipt[^"]*"[^>]*data-ui', hero) and 'id="feed"' in hero
    assert re.search(r'<a class="btn btn-primary" id="herotry" href="#try">Try to trick the agent</a>', hero)
    assert '<a class="t-link" href="#how">See how it works ↓</a>' in hero
    assert "PayPal sandbox · test money, no real charges." in hero
    assert "<span>Let your AI agent shop.</span> <span>Keep your rules.</span>" in hero
    nav = HTML[HTML.index('<nav class="nav"'):HTML.index("</nav>")]
    assert 'id="navlive"' in nav and 'href="#try">Try to trick the agent</a>' in nav


def test_stamp_vocabulary_and_honest_replay_wording():
    for w in ("PAID", "HELD", "BLOCKED", "REFUNDED"):
        assert w in JS, w
    # eval replays are dry runs: an allowed replay line is never stamped PAID (no money moved)
    assert "dry run" in HTML.lower() or "dry run" in JS.lower()
    assert "live ? ['paid', 'PAID'] : ['pass', 'ALLOWED']" in JS


def test_sections_in_design_order_and_existing_selectors_kept():
    # landing-copy v2 order: hero, problem, how, try it (compare panel, Gauntlet, demo shop), proof, why now, scenarios, developers
    order = [HTML.index(s) for s in ('<header class="hero"', 'id="problem"', 'id="how"', 'id="try"', 'id="compare"', 'id="gbtn"',
                                     'id="workspace"', 'id="proof"', 'id="whynow"', 'id="showcase"', 'id="developers"', 'id="mcp"', "<footer")]
    assert order == sorted(order)
    for sel in ('id="bentomcp"', 'id="dguided"', 'id="mcp"', 'id="dbusy"', 'id="dsched"', 'id="navlive"',
                'id="howbar"', 'id="stage"', 'id="scoretbl"', 'id="gbtn"', 'id="cmpl"', 'id="cmpr"', 'id="feed"'):
        assert sel in HTML, sel
    assert "by WebIntel Tools" not in HTML


def test_two_large_close_crop_screenshots_are_served(tmp_path):
    """Review: two large, close crops (half the page each), not four narrow ones; captured at 2x so they stay sharp."""
    c = _client(tmp_path)
    shots = re.findall(r'<img[^>]+src="(/static/img/[^"]+)"[^>]*alt="([^"]+)"[^>]*width="(\d+)" height="(\d+)"', HTML)
    assert len(shots) == 2
    for src, alt, w, h in shots:
        r = c.get(src)
        assert r.status_code == 200 and r.headers["content-type"] in ("image/webp", "image/jpeg", "image/png"), src
        assert len(alt) > 10 and int(w) >= 1200, src
    assert ".shots{display:grid;grid-template-columns:repeat(2,minmax(0,1fr))" in CSS


def test_no_gradients_and_no_emoji_or_symbol_glyphs_as_ui():
    """Review: remove the 3 gradients (the sawtooth masks were conic-gradients) and the 5 emoji used as UI."""
    sched = (ROOT / "static/schedule.js").read_text()
    for name, src in (("app.css", CSS), ("dashboard.html", HTML), ("app.js", JS), ("schedule.js", sched)):
        assert "gradient" not in src.lower(), name
        assert not re.search("[\u2713\u2715\u21BA\u2726\u25D4\U0001F300-\U0001FAFF]", src), name


def test_developer_list_has_the_tooth_marker_and_links_into_the_readme():
    """landing-copy v2 section 8 (replaces the six-item feature list): four lines, each a link into the README, with the
    sawtooth tick as the bullet and no boxes; the MCP line links to the connect panel."""
    dev = HTML[HTML.index('<ul class="ticks dev-list">'):HTML.index("</ul>", HTML.index('<ul class="ticks dev-list">'))]
    items = re.findall(r"<li>(.*?)</li>", dev, re.S)
    assert [re.search(r"<b>([^<]+)</b>", i).group(1) for i in items] == ["PayPal:", "Claude guard:", "MCP:", "Safety:"]
    assert all('href="https://github.com/JordiBen/agenttab#' in i for i in items)
    assert 'id="bentomcp"' in items[2] and 'href="#mcp"' in items[2]
    assert not re.search(r'class="tile[ "]', HTML) and 'class="bento' not in HTML and 'class="features' not in HTML
    rule = CSS[CSS.index(".ticks li::before"):].split("}")[0]
    assert "clip-path:polygon(0 0,100% 0,50% 100%)" in rule
    li = CSS[CSS.index(".ticks li{"):].split("}")[0]
    assert "background" not in li and "border" not in li


def test_ag_grid_and_bryntum_containers_are_data_ui():
    for gid in ("dsched", "dpurch", "dledger", "agents", "osched", "opurch", "ledger"):
        assert re.search(rf'<div id="{gid}" class="(grid-h|sched)" data-ui', HTML), gid
    assert re.search(r'<div class="tbl-wrap[^"]*" data-ui><table class="tbl[^"]*" id="scoretbl">', HTML)


def test_no_hairline_borders_outside_data_ui_and_the_receipt_tear_is_sawtooth():
    """Taste lint: a 1px (or thinner) border is a hairline rule. Only real table chrome inside [data-ui] may use one, and says
    so on its line. The receipt's tear line is the sawtooth motif (a filled shape), not a dashed border."""
    bad = [ln.strip() for ln in CSS.splitlines()
           if re.search(r"border(-top|-bottom|-left|-right)?\s*:\s*(0?\.\d+|1)px", ln) and "inside [data-ui]" not in ln]
    assert bad == []
    tear = CSS[CSS.index(".term-body::before{"):].split("}")[0]
    assert "var(--saw-down)" in tear and "mask:" in tear


# ---------------- landing-copy v2 (final page text) ----------------
FORBIDDEN = [r"\bPOST\b", r"\blayer\b", r"policy-ai", r"decide\(\)", r"Orders v2", r"micro-dollar", r"SSRF", r"Streamable HTTP",
             r"webhook", r"p50", r"p95", r"eval run id", r"heuristic"]
_CI = {r"\bPOST\b"}                                    # POST is the HTTP verb: case-sensitive ("post" is an English word)


def forbidden_in(text: str) -> list[str]:
    return [w for w in FORBIDDEN if re.search(w, text, 0 if w in _CI else re.I)]


def _visible_text(html: str) -> str:
    html = re.sub(r"<(script|style)\b.*?</\1>", " ", html, flags=re.S | re.I)
    html = re.sub(r"<!--.*?-->", " ", html, flags=re.S)
    return re.sub(r"<[^>]+>", " ", html)


def test_forbidden_words_list_catches_each_word():
    for w in ("POST purchase", "layer: caps", "policy-ai", "decide()", "Orders v2", "micro-dollar", "SSRF", "Streamable HTTP",
              "webhooks", "p50", "p95 ms", "eval run id", "(heuristic mode)"):
        assert forbidden_in(w), w
    assert forbidden_in("a launch post, players, a decided rule") == []


def test_no_forbidden_words_on_the_page_outside_the_developer_section():
    """landing-copy v2: the words to avoid (POST, layer, policy-ai, decide(), Orders v2, ...) never appear outside section 8,
    in the static page text (the page as rendered by app.js is checked in tests/test_e2e_landing.py)."""
    body = HTML[HTML.index("<body>"):]
    dev0 = body.index('<section id="developers"'); dev1 = body.index("</section>", dev0)
    outside = body[:dev0] + body[dev1:]
    assert forbidden_in(_visible_text(outside)) == []
    for attr in re.findall(r'(?:aria-label|title|alt|placeholder)="([^"]*)"', outside):
        assert forbidden_in(attr) == [], attr
    # the developer section is where the technical words belong
    assert forbidden_in(_visible_text(body[dev0:dev1]))


def test_ai_status_labels_follow_the_copy():
    for txt in ("live: 'AI guard: on'", "busy: 'AI guard busy: purchases wait for you'", "off: 'AI guard off (local preview)'"):
        assert txt in JS, txt
    for old in ("'Live AI guard'", "'AI busy, human approval'", "'Keyword mode (no AI configured)'"):
        assert old not in JS, old


def test_keyword_check_reason_says_keyword_check_not_heuristic():
    """The keyword check's own reason is shown on the Try it receipts and in the Gauntlet: plain words, no 'heuristic'."""
    from app import procurement as pr
    cfg = pr.AgentConfig(pr.SHOP_POLICY, dict(pr.SHOP_VENDOR_BUDGETS), pr.SHOP_APPROVAL_THRESHOLD, pr.SHOP_PER_PURCHASE_MAX, pr.SHOP_DAILY_CAP)
    d = pr.decide(cfg, pr.Purchase("samplebox", "ceramic-mug-sample", 1, "Quality-check a stoneware mug from a supplier before we stock it", ""),
                  pr.State(), "keyword", None)
    assert d.outcome == "allow" and "heuristic" not in d.reason.lower() and "keyword check" in d.reason


def test_page_numbers_are_not_typed_into_the_html():
    """Every proof number is read from eval/results.json or eval/heldout-results.json at render time."""
    sec = HTML[HTML.index('<section id="proof"'):HTML.index("</section>", HTML.index('<section id="proof"'))]
    assert not re.search(r"\d", _visible_text(sec)), "no digits typed into the proof section"
    for n in ("37", "47", "26 / 26", "14 / 14", "40 cases"):
        assert n not in _visible_text(HTML[HTML.index('<section id="proof"'):HTML.index('<section id="whynow"')]), n


def test_scorecard_serves_the_held_out_summary_and_the_test_count(tmp_path):
    j = _client(tmp_path).get("/api/scorecard").json()
    res = json.loads((ROOT / "eval/heldout-results.json").read_text())
    run = json.loads((ROOT / "eval/heldout-run.json").read_text())
    h = j["heldout"]
    assert h["cases"] == len(res["ai"]["rows"]) == len(json.loads((ROOT / "eval/heldout.json").read_text()))
    assert h["runs"] == 1 and h["git_sha"] == run["git_sha"]
    for mode in ("none", "keyword", "ai"):
        for k in ("attacks_stopped", "attacks_total", "legit_correct", "legit_total", "false_blocks"):
            assert h["modes"][mode][k] == res[mode]["summary"][k], (mode, k)
    assert "rows" not in json.dumps(h)                       # summary only: no case text leaves the file
    assert isinstance(j["test_count"], int) and j["test_count"] > 0


def test_test_count_equals_what_pytest_collects():
    import subprocess, sys
    from app.main import TEST_COUNT, count_tests
    out = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", str(ROOT / "tests")],
                         cwd=ROOT, capture_output=True, text=True).stdout
    collected = int(re.search(r"(\d+) tests? collected", out).group(1))
    assert TEST_COUNT == count_tests(ROOT / "tests") == collected


def test_why_now_facts_each_carry_a_source_link():
    """Three facts, each verified against its source page (see the branch notes); a fact that could not be verified is dropped."""
    sec = HTML[HTML.index('<section id="whynow"'):HTML.index("</section>", HTML.index('<section id="whynow"'))]
    items = re.findall(r"<li>(.*?)</li>", sec, re.S)
    assert len(items) == 3 and all(re.search(r'<a href="https://[^"]+">[^<]+</a>', i) for i in items)
    assert "61%" in items[0] and "84%" in items[1] and "Agent Ready" in items[2]
    assert "58%" not in sec                                   # the source says 59%; the 58% clause was dropped, not retyped
