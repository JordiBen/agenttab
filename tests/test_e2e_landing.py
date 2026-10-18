"""Browser e2e for the redesigned landing page (docs/DESIGN.md): proof tiles read results.json, the receipt prints real
replay lines with stamps, the showcase swaps verdicts, the primary action runs the injected purchase, and the page holds
at 1440 / 768 / 390 with no horizontal scroll and no page errors. Keyword mode, mock PayPal: no AI calls."""
import json, os, shutil, socket, subprocess, sys, time
from pathlib import Path
import httpx, pytest

pw = pytest.importorskip("playwright.sync_api")
CHROME = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
pytestmark = pytest.mark.skipif(not CHROME and not os.environ.get("PW_BUNDLED"), reason="no Chrome for Playwright")
ROOT = Path(__file__).resolve().parent.parent


def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    port = _free_port(); base = f"http://127.0.0.1:{port}"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PAYPAL_", "GEMINI_", "LLM_", "ANTHROPIC_"))}
    env.update(ADMIN_TOKEN="e2e-admin-token-not-secret", PAYPAL_MODE="mock", ALLOW_INSECURE_MOCK="1", DEMO_MODE="1",
               PUBLIC_URL=base, AGENTTAB_DB=str(tmp_path_factory.mktemp("land") / "l.db"))
    p = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port)], cwd=ROOT, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            if httpx.get(base + "/healthz", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.1)
    yield base
    p.terminate(); p.wait(5)


@pytest.fixture(scope="module")
def browser():
    with pw.sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROME, args=["--no-sandbox"]) if CHROME else p.chromium.launch()
        yield b
        b.close()


def _open(browser, server, w, h, **kw):
    pg = browser.new_page(viewport={"width": w, "height": h}, is_mobile=w < 500, **kw)
    pg.errs = []; pg.on("pageerror", lambda e: pg.errs.append(str(e)))
    pg.goto(server + "/"); pg.wait_for_selector("#demo", state="visible")
    return pg


def test_proof_table_comes_from_the_results_files_with_previous_catalog_label(server, browser):
    """landing-copy v2 section 5 (replaces the proof tiles): one table, three rows, every number from eval/results.json, plus
    the held-out row from eval/heldout-results.json, the indirect-injection sentence and the footnote counts."""
    pg = _open(browser, server, 1440, 900)
    sc = httpx.get(server + "/api/scorecard").json()
    res = json.loads((ROOT / "eval/results.json").read_text(encoding="utf-8")); held = json.loads((ROOT / "eval/heldout-results.json").read_text(encoding="utf-8"))
    pg.wait_for_function("document.querySelectorAll('#scoretbl tbody tr').length === 3")
    rows = pg.eval_on_selector_all("#scoretbl tbody tr", "trs => trs.map(t => [...t.cells].map(c => c.innerText.replace(/\\s+/g, ' ').trim()))")
    for (mode, name), row in zip((("none", "No guard"), ("keyword", "Rules only"), ("ai", "Rules + Claude")), rows):
        m = res["modes"][mode]; stale = " previous catalog" if mode in res.get("stale_modes", []) else ""
        assert row == [name + stale, f"{m['attacks_stopped']} / {m['attacks_total']}", f"{m['legit_correct']} / {m['legit_total']}", str(m["false_blocks"])]
    ind = res["by_category"]["indirect_injection"]
    assert f"{ind['keyword']} of {ind['total']} with rules alone, {ind['ai']} of {ind['total']} with Claude." in pg.inner_text("#proofline")
    ai, kw = held["ai"]["summary"], held["keyword"]["summary"]; n = len(held["ai"]["rows"])
    assert pg.is_visible("#heldout") and pg.inner_text("#heldout") == (
        f"A test the builder never saw: Claude stopped {ai['attacks_stopped']} / {ai['attacks_total']} attacks and allowed "
        f"{ai['legit_correct']} / {ai['legit_total']} legit buys, {ai['false_blocks']} wrongly blocked. "
        f"Rules alone: {kw['attacks_stopped']} / {kw['attacks_total']}. ({n} cases written separately, run once.)")
    # the reviewer's wording, checked once against today's file so a silent number change shows up here too
    assert pg.inner_text("#heldout") == ("A test the builder never saw: Claude stopped 26 / 26 attacks and allowed 14 / 14 legit buys, "
                                         "0 wrongly blocked. Rules alone: 19 / 26. (40 cases written separately, run once.)")
    foot = pg.inner_text("#prooffoot")
    assert foot.startswith(f"{res['cases']} labeled purchases") and f"the separate {len(held['ai']['rows'])}-case set" in foot
    assert pg.inner_text("#hown") == str(res["cases"]) and pg.inner_text("#testcount").strip() == str(sc["test_count"])
    assert pg.errs == []


def test_receipt_prints_real_replay_lines_with_stamps_and_never_paid_for_a_dry_run(server, browser):
    pg = _open(browser, server, 1440, 900)
    pg.wait_for_function("document.querySelectorAll('#feed .fe .stamp').length >= 2")
    stamps = pg.eval_on_selector_all("#feed .stamp", "els => els.map(e => e.textContent)")
    assert set(stamps) <= {"ALLOWED", "BLOCKED", "HELD"} and "PAID" not in stamps
    ids = {d["id"] for d in httpx.get(server + "/api/replay").json()["decisions"]}
    assert len(ids) == 67


def test_showcase_swaps_to_the_real_verdict_of_each_scenario(server, browser):
    pg = _open(browser, server, 1440, 900)
    by = {d["id"]: d for d in httpx.get(server + "/api/replay").json()["decisions"]}
    pg.wait_for_function("document.querySelector('#stage .stamp')")
    for case in ("forbidden_vendor-04", "legit-15", "social_target-01"):
        pg.click(f"#scen button[data-case='{case}']"); pg.wait_for_timeout(400)
        assert pg.get_attribute(f"#scen button[data-case='{case}']", "aria-selected") == "true"
        word = {"block": "BLOCKED", "approval": "HELD", "allow": "ALLOWED"}[by[case]["outcome"]]
        # landing-copy v2: who decided, in plain words; the layer name stays in the API
        who = {"block": "Stopped by: ", "approval": "Waiting for: owner approval", "allow": ""}[by[case]["outcome"]]
        stage = pg.inner_text("#stage")
        assert pg.inner_text("#stage .stamp") == word and who in stage
        assert "layer" not in stage.lower() and "policy-" not in stage and "allowlist" not in stage
        assert by[case]["purpose"] in stage


def test_primary_action_jumps_to_try_it_and_the_demo_shop_still_runs_the_injected_purchase(server, browser):
    # landing-copy v2: "Try to trick the agent" jumps to section 4 (the compare panel); the guided demo stays below it.
    pg = _open(browser, server, 1440, 900)
    pg.click("#herotry"); pg.wait_for_timeout(1200)
    assert pg.evaluate("location.hash") == "#try"
    top = pg.evaluate("document.getElementById('try').getBoundingClientRect().top")
    assert -5 <= top < 200, top
    assert pg.is_visible("#cmpbtn")
    pg.click("text=1. Start demo session"); pg.wait_for_function("sessionStorage.dkey")
    with pg.expect_request(lambda r: r.url.endswith("/api/purchases") and r.method == "POST", timeout=20000) as req:
        pg.click("text=6. Try an injected instruction")
    body = json.loads(req.value.post_data)
    assert body["target"] == "https://example.com/partner-offer" and "instruction" in body["purpose"].lower()
    assert pg.evaluate("sessionStorage.dkey")


@pytest.mark.parametrize("w,h", [(1440, 900), (768, 1024), (390, 844)])
def test_no_horizontal_scroll_and_no_errors(server, browser, w, h):
    pg = _open(browser, server, w, h)
    pg.wait_for_timeout(800)
    assert pg.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert pg.errs == []


def test_reduced_motion_prints_the_receipt_instantly(server, browser):
    pg = _open(browser, server, 1440, 900, reduced_motion="reduce")
    pg.wait_for_function("document.querySelectorAll('#feed .fe').length >= 2")
    assert pg.evaluate("[...document.querySelectorAll('#feed .res')].every(r => !r.hidden)")
    assert pg.evaluate("getComputedStyle(document.querySelector('.reveal')).opacity") == "1"


# ---------------- review fixes (landing v2) ----------------
AUDIT = r"""() => {
 const vis = e => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e); return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
 const name = e => (e.id ? '#' + e.id : '') + (e.className && e.className.baseVal === undefined ? '.' + String(e.className).split(' ').slice(0, 2).join('.') : '') + '<' + e.tagName.toLowerCase();
 const out = {grad: [], hair: [], small: [], body: []};
 for (const e of document.querySelectorAll('body *')) {
  if (!vis(e)) continue; const s = getComputedStyle(e); const inUI = !!e.closest('[data-ui]');
  if (/gradient/.test(s.backgroundImage + ' ' + (s.maskImage || '') + ' ' + (s.webkitMaskImage || ''))) out.grad.push(name(e));
  if (!inUI) for (const side of ['Top', 'Right', 'Bottom', 'Left']) { const w = parseFloat(s['border' + side + 'Width']);
    if (w > 0 && w < 2 && s['border' + side + 'Style'] !== 'none') { out.hair.push(name(e)); break; } }
  if ([...e.childNodes].some(n => n.nodeType === 3 && n.textContent.trim())) { const fs = parseFloat(s.fontSize);
    if (fs < 13) out.small.push(name(e) + ' ' + fs);
    if (e.tagName === 'P' && !inUI && fs < 16) out.body.push(name(e) + ' ' + fs); }
 }
 return out; }"""


@pytest.mark.parametrize("w,h", [(1440, 900), (390, 844)])
def test_design_audit_no_gradients_hairlines_or_small_text(server, browser, w, h):
    """Review (d): fills instead of 1px outline boxes (hairlines only inside [data-ui]), no gradients, body copy >= 16px,
    no readable text under 13px. Run with the owner console open so its forms and grids are measured too."""
    pg = _open(browser, server, w, h)
    pg.evaluate("document.querySelectorAll('.reveal').forEach(e => e.classList.add('in')); document.getElementById('ownerhint').open = true")
    pg.wait_for_function("document.querySelector('#agents .ag-root-wrapper') && document.querySelector('.b-scheduler')", timeout=20000)
    pg.wait_for_timeout(600)
    r = pg.evaluate(AUDIT)
    assert r == {"grad": [], "hair": [], "small": [], "body": []}
    assert pg.evaluate("[...document.querySelectorAll('.ag-root-wrapper, .b-scheduler')].every(e => e.closest('[data-ui]'))")
    pg.close()


@pytest.mark.parametrize("w,h", [(1440, 900), (1280, 800), (390, 844)])
def test_owner_console_open_does_not_overflow_or_overlap_headings(server, browser, w, h):
    pg = _open(browser, server, w, h)
    pg.evaluate("document.getElementById('ownerhint').open = true"); pg.wait_for_timeout(800)
    assert pg.evaluate("document.documentElement.scrollWidth <= innerWidth")
    a = pg.locator("#ownerhint h3", has_text="Agents").bounding_box(); g = pg.locator("#agents").bounding_box()
    assert a["y"] + a["height"] <= g["y"] and a["x"] >= 0 and a["x"] + a["width"] <= w
    pg.close()


@pytest.mark.parametrize("w,h", [(1440, 900), (390, 844)])
def test_receipt_lines_wrap_inside_the_receipt(server, browser, w, h):
    """Review (a): hero receipt lines were cut at the right edge; every line must wrap inside the paper."""
    pg = _open(browser, server, w, h)
    pg.evaluate("document.querySelector('.receipt').scrollIntoView({behavior: 'instant'})")   # the feed pauses off-screen
    pg.wait_for_function("document.querySelectorAll('#feed .fe .res:not([hidden])').length >= 1", timeout=40000)
    bad = pg.evaluate("""() => { const rc = document.querySelector('.receipt').getBoundingClientRect(); const out = [];
      if (rc.right > innerWidth + 0.5) out.push('receipt past viewport ' + rc.right);
      for (const e of document.querySelectorAll('#feed .fe, #feed .fe *')) { const r = e.getBoundingClientRect();
        if (r.width && r.right > rc.right + 0.5) out.push(e.className + ' right ' + Math.round(r.right));
        if (e.scrollWidth > e.clientWidth + 1 && getComputedStyle(e).overflow !== 'visible') out.push(e.className + ' clipped'); }
      return out; }""")
    assert bad == []
    pg.close()


@pytest.mark.parametrize("w,h", [(1440, 900), (390, 844)])
def test_h1_is_at_most_two_lines(server, browser, w, h):
    pg = _open(browser, server, w, h)
    lines = pg.evaluate("""() => { const h = document.querySelector('h1'); const s = getComputedStyle(h);
      return Math.round(h.getBoundingClientRect().height / parseFloat(s.lineHeight)); }""")
    assert lines <= 2
    assert " ".join(pg.inner_text("h1").split()) == "Let your AI agent shop. Keep your rules."
    pg.close()


@pytest.mark.parametrize("w,h", [(1440, 900), (1024, 800), (768, 1024)])
def test_how_it_works_captions_have_room(server, browser, w, h):
    """Review (c): no one-word-per-line captions: each caption sits below the bar with at least ~13rem of width."""
    pg = _open(browser, server, w, h)
    widths = pg.eval_on_selector_all(".how-caps li", "els => els.map(e => e.getBoundingClientRect().width)")
    assert len(widths) == 4 and min(widths) >= 200                 # landing-copy v2: four steps (was five)
    assert pg.evaluate("document.querySelectorAll('#howbar p').length") == 0
    pg.close()


def test_two_screenshots_each_half_the_page_wide(server, browser):
    pg = _open(browser, server, 1440, 900)
    pg.evaluate("document.querySelector('.shots').scrollIntoView({behavior: 'instant'})"); pg.wait_for_timeout(400)
    r = pg.evaluate("""() => { const s = document.querySelector('.shots').getBoundingClientRect();
      return [...document.querySelectorAll('.shots .shot')].map(f => f.getBoundingClientRect().width / s.width); }""")
    assert len(r) == 2 and all(x >= 0.48 for x in r)
    pg.close()


def test_developer_list_has_no_boxes(server, browser):
    # landing-copy v2: the six-item feature list became the four-line For developers list (same tooth marker, no boxes)
    pg = _open(browser, server, 1440, 900)
    r = pg.eval_on_selector_all(".dev-list li", """els => els.map(e => { const s = getComputedStyle(e), b = getComputedStyle(e, '::before');
      return [s.backgroundColor, s.borderTopStyle, b.content !== 'none' && parseFloat(b.width) <= 14]; })""")
    assert len(r) == 4 and all(bg in ("rgba(0, 0, 0, 0)", "transparent") and bs == "none" and marker for bg, bs, marker in r)
    pg.close()


# ---- review round 3 ----

_COVERED = """sel => { const el = [...document.querySelectorAll(sel[0])].find(e => e.textContent.trim().startsWith(sel[1]));
  const r = el.getBoundingClientRect(); const out = [];
  for (const x of [r.left + 2, r.left + r.width / 2, r.right - 2]) for (const y of [r.top + 2, r.top + r.height / 2, r.bottom - 2]) {
    if (y < 0 || y >= innerHeight || x < 0 || x >= innerWidth) continue;
    const t = document.elementFromPoint(x, y); if (t && !el.contains(t)) out.push(Math.round(y) + ':' + t.tagName + '.' + t.className); }
  return [Math.round(r.top), out]; }"""


def test_sticky_nav_never_covers_the_agents_heading_while_scrolling_down(server, browser):
    """Review 3 (a): at 1440 the sticky nav sat over the owner console's 'Agents' heading while scrolling down."""
    pg = _open(browser, server, 1440, 900)
    pg.evaluate("document.getElementById('ownerhint').open = true"); pg.wait_for_timeout(800)
    y0 = pg.evaluate("[...document.querySelectorAll('#ownerhint h3')].find(e => e.textContent.trim() === 'Agents').getBoundingClientRect().top + scrollY")
    pg.evaluate(f"scrollTo(0, {y0 - 400})"); pg.wait_for_timeout(300); pg.mouse.move(700, 450)
    seen = []
    for _ in range(12):                                   # wheel down until the heading sits in the nav's band at the top
        pg.mouse.wheel(0, 40); pg.wait_for_timeout(350)
        top, covered = pg.evaluate(_COVERED, ["#ownerhint h3", "Agents"])
        if 0 <= top <= 70:
            seen.append(top); assert covered == [], f"Agents heading covered at top={top}: {covered}"
    assert seen, "the heading never reached the top band"
    pg.close()


def test_anchor_jumps_land_below_the_sticky_nav(server, browser):
    pg = _open(browser, server, 1440, 900)
    pg.evaluate("location.hash = '#proof'")
    pg.wait_for_function("""() => { const y = scrollY, w = window; const ok = w.__lastY === y && y > 0; w.__lastY = y; return ok; }""", polling=250, timeout=10000)
    top = pg.evaluate("document.getElementById('proof').getBoundingClientRect().top")
    nav = pg.evaluate("document.querySelector('.nav').offsetHeight")   # full nav height, tucked or not
    assert top >= nav - 1, (top, nav)
    pg.close()


@pytest.mark.parametrize("w,h", [(1440, 900), (768, 1024), (390, 844)])
def test_proof_numbers_sit_in_their_row_with_the_name_first_not_an_eyebrow(server, browser, w, h):
    """Review 3 (b) for the table that replaced the tiles: each mode's name and its numbers share one row (no small label
    stacked above a number), numbers at least 16px, and the table fits the screen."""
    pg = _open(browser, server, w, h)
    pg.wait_for_function("document.querySelectorAll('#scoretbl tbody tr').length === 3")
    rows = pg.evaluate("""() => [...document.querySelectorAll('#scoretbl tbody tr')].map(t => { const c = [...t.cells].map(x => x.getBoundingClientRect());
      return {same: c.every(r => r.top === c[0].top), px: [...t.cells].slice(1).map(x => parseFloat(getComputedStyle(x).fontSize))}; })""")
    assert len(rows) == 3 and all(r["same"] and min(r["px"]) >= 16 for r in rows), rows
    assert pg.evaluate("document.getElementById('scoretbl').getBoundingClientRect().right <= innerWidth + 0.5")
    pg.close()


_RGB = """c => { const m = c.match(/^rgba?\\(([^)]+)\\)/); if (m) return m[1].split(/[ ,\\/]+/).filter(Boolean).slice(0, 3).map(Number);
  const s = c.match(/^color\\(srgb ([^)]+)\\)/); if (s) return s[1].split(/\\s+/).slice(0, 3).map(v => Math.round(parseFloat(v) * 255));
  return null; }"""


def _contrast(a, b):
    def lum(c):
        cs = [v / 255 for v in c]
        cs = [v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4 for v in cs]
        return 0.2126 * cs[0] + 0.7152 * cs[1] + 0.0722 * cs[2]
    hi, lo = sorted([lum(a), lum(b)], reverse=True)
    return (hi + 0.05) / (lo + 0.05)


@pytest.mark.parametrize("w,h", [(1440, 900), (1024, 800)])
def test_how_bar_segment_text_has_at_least_4_5_contrast(server, browser, w, h):
    """Review 3 (c): 'AI guard' / '23 of 67' on the bar were ~3:1; every segment label must reach WCAG AA 4.5:1."""
    pg = _open(browser, server, w, h)
    pg.wait_for_timeout(1500)
    segs = pg.evaluate(f"""() => {{ const rgb = {_RGB}; return [...document.querySelectorAll('#howbar .seg .fill')].map(f => {{
      const s = getComputedStyle(f); return {{text: f.textContent.trim(), fg: rgb(s.color), bg: rgb(s.backgroundColor), raw: [s.color, s.backgroundColor]}}; }}); }}""")
    assert len(segs) == 5
    for s in segs:
        assert s["fg"] and s["bg"], s["raw"]
        assert _contrast(s["fg"], s["bg"]) >= 4.5, (s["text"], s["raw"], round(_contrast(s["fg"], s["bg"]), 2))
    pg.close()


@pytest.mark.parametrize("w,h", [(1440, 900), (768, 1024), (390, 844)])
def test_hero_receipt_has_no_empty_area(server, browser, w, h):
    """Review 3 (d): the fixed-height receipt left a big empty area; the paper must stay full of lines, before and after the feed moves."""
    pg = _open(browser, server, w, h)
    pg.evaluate("document.querySelector('.receipt').scrollIntoView({behavior: 'instant', block: 'center'})")
    pg.wait_for_function("document.querySelectorAll('#feed .fe').length >= 2", timeout=40000)
    gaps = """() => { const b = document.getElementById('feed').getBoundingClientRect(), k = document.getElementById('feed').children;
      return {top: k[0].getBoundingClientRect().top - b.top, bottom: b.bottom - k[k.length - 1].getBoundingClientRect().bottom, h: b.height}; }"""
    for _ in range(2):
        g = pg.evaluate(gaps)
        assert g["h"] > 200 and g["top"] <= 1 and g["bottom"] <= 2, g
        pg.wait_for_timeout(4200)                          # one feed step (3.4 s) prints a new line
    g = pg.evaluate(gaps)
    assert g["top"] <= 1 and g["bottom"] <= 2, g
    pg.close()


@pytest.mark.parametrize("direction", ["down", "up"])
def test_agents_heading_is_never_covered_arriving_from_either_direction(server, browser, direction):
    """Taste inspect: the owner console's 'Agents' heading was covered by the nav when it sat in the top band, notably
    when scrolling back up. Nothing may cover it at any offset, from either direction."""
    pg = _open(browser, server, 1440, 900)
    pg.evaluate("document.getElementById('ownerhint').open = true"); pg.wait_for_timeout(800)
    y0 = pg.evaluate("[...document.querySelectorAll('#ownerhint h3')].find(e => e.textContent.trim() === 'Agents').getBoundingClientRect().top + scrollY")
    for off in (0, 10, 30, 50):
        start = y0 - off + (600 if direction == "up" else -600)
        pg.evaluate(f"document.documentElement.style.scrollBehavior = 'auto'; scrollTo(0, {start})"); pg.wait_for_timeout(250)
        pg.evaluate(f"scrollTo(0, {y0 - off})"); pg.wait_for_timeout(450)
        top, covered = pg.evaluate(_COVERED, ["#ownerhint h3", "Agents"])
        assert covered == [], f"{direction} offset {off}: heading top={top} covered by {covered}"
    pg.close()


# ---------------- landing-copy v2 ----------------
_QUOTED = "#feed .pur, #stage .purp, .cmp-text, .g-row .p, #probq, #dpolicy, #dout, .ag-root-wrapper, .b-scheduler, #developers"


def test_rendered_page_has_no_forbidden_words_outside_the_developer_section(server, browser):
    """landing-copy v2 "words to avoid", checked on the page as rendered by app.js: hero receipt, scenarios, a compare
    run, a Gauntlet run and a demo purchase. Quoted data (the agent's purposes and targets, grids, the raw console) and the
    For developers section are excluded: the list is about our words, not about the attack texts."""
    from tests.test_landing import forbidden_in
    pg = _open(browser, server, 1440, 900)
    pg.wait_for_function("document.querySelectorAll('#cmpcase option').length > 1")
    pg.click("#cmpbtn"); pg.wait_for_selector("#cmpr .stamp")
    pg.select_option("#cmpcase", "legit-01") if pg.locator("#cmpcase option[value='legit-01']").count() else None
    pg.wait_for_timeout(200); pg.click("#cmpbtn"); pg.wait_for_timeout(800)
    pg.click("#gbtn"); pg.wait_for_function("document.querySelectorAll('#glist .g-row').length >= 10", timeout=20000)
    pg.click("text=1. Start demo session"); pg.wait_for_function("sessionStorage.dkey")
    pg.click("text=5. Try a gift card"); pg.wait_for_function("document.getElementById('dverdict').textContent.includes('Blocked')")
    pg.wait_for_function("document.querySelectorAll('#feed .fe .who').length >= 1", timeout=20000)
    text = pg.evaluate(f"""() => {{ const c = document.body.cloneNode(true);
      c.querySelectorAll('{_QUOTED}, script, style').forEach(e => e.remove()); return c.innerText; }}""")
    assert forbidden_in(text) == [], forbidden_in(text)
    assert "Stopped by:" in text and "Rules only" in text and "Nothing was charged" in text   # the rendered parts were there
    pg.close()


def test_problem_example_shows_the_real_verdicts_of_its_eval_case(server, browser):
    pg = _open(browser, server, 1440, 900)
    res = json.loads((ROOT / "eval/results.json").read_text(encoding="utf-8"))
    case = pg.get_attribute("#probex", "data-case")
    got = {m: next(r["got"] for r in res["modes"][m]["rows"] if r["id"] == case) for m in ("none", "ai")}
    word = {"allow": "BOUGHT", "block": "BLOCKED", "approval": "HELD"}
    pg.wait_for_function("document.getElementById('probai').textContent !== '…' && document.getElementById('probq').textContent !== '…'")
    assert pg.inner_text("#probnone") == word[got["none"]] and pg.inner_text("#probai") == word[got["ai"]]
    purpose = next(c["purpose"] for c in json.loads((ROOT / "eval/cases.json").read_text(encoding="utf-8")) if c["id"] == case)
    assert pg.inner_text("#probq") in purpose and len(pg.inner_text("#probq")) > 20
    pg.close()


def test_status_pill_reads_the_copy_label(server, browser):
    pg = _open(browser, server, 390, 844)                          # visible on a phone too: the pill is in the top bar
    pg.wait_for_function("document.getElementById('navlive').dataset.status")
    st = pg.get_attribute("#navlive", "data-status")
    assert pg.inner_text("#navlive") == {"live": "AI guard: on", "busy": "AI guard busy: purchases wait for you",
                                         "off": "AI guard off (local preview)"}[st]
    assert pg.is_visible("#navlive")
    pg.close()


def test_vendor_budget_bars_still_render_after_the_proof_section_rewrite(server, browser):
    # The proof rewrite dropped the category bars and, with them, the shared .track rule the demo's vendor budgets use.
    pg = _open(browser, server, 1440, 900)
    pg.click("text=1. Start demo session"); pg.wait_for_function("document.querySelector('#dbudgets .track')", timeout=20000)
    r = pg.evaluate("""() => [...document.querySelectorAll('#dbudgets .track')].map(t => { const b = t.getBoundingClientRect();
      return [b.width, b.height, getComputedStyle(t).backgroundColor]; })""")
    assert r and all(w >= 20 and h >= 4 and bg != "rgba(0, 0, 0, 0)" for w, h, bg in r), r
    pg.close()


@pytest.mark.parametrize("w,h", [(1440, 900), (768, 1024), (390, 844)])
def test_problem_headline_fits_in_two_lines(server, browser, w, h):
    pg = _open(browser, server, w, h)
    lines = pg.evaluate("""() => { const e = document.querySelector('#problem h2'), s = getComputedStyle(e);
      const lh = parseFloat(s.lineHeight) || 1.2 * parseFloat(s.fontSize);
      return [e.textContent, e.getBoundingClientRect().height / lh]; }""")
    assert lines[0] == "One web page can spend your money." and lines[1] <= 2.05, lines
    pg.close()


def test_try_it_links_down_to_the_full_demo_shop(server, browser):
    pg = _open(browser, server, 1440, 900)
    pg.locator("#demolink").scroll_into_view_if_needed(); pg.click("#demolink"); pg.wait_for_timeout(1200)
    assert pg.evaluate("location.hash") == "#demoshop"
    assert -5 <= pg.evaluate("document.getElementById('demoshop').getBoundingClientRect().top") < 200
    order = pg.evaluate("[...document.querySelectorAll('main > section, body > section')].map(s => s.id)")
    assert order.index("whynow") < order.index("demoshop") < order.index("developers"), order
    pg.close()


@pytest.mark.parametrize("w,h", [(1440, 900), (768, 1024), (390, 844)])
def test_owner_fields_fill_whole_rows_so_no_label_sits_alone_like_an_eyebrow(server, browser, w, h):
    """Review 5: at 768 'Per-call cap $' wrapped onto a row of its own under the other three fields."""
    pg = _open(browser, server, w, h)
    pg.evaluate("document.getElementById('ownerhint').open = true"); pg.wait_for_timeout(400)
    tops = pg.eval_on_selector_all("#ownerhint .ofields > div", "els => els.map(e => Math.round(e.getBoundingClientRect().top))")
    rows = {}
    for t in tops: rows[t] = rows.get(t, 0) + 1
    assert len(tops) == 4 and len(set(rows.values())) == 1, rows               # 4, 2+2 or 1+1+1+1: never 3+1
    assert {1440: 1, 768: 2, 390: 4}[w] == len(rows), rows
    pg.close()


@pytest.mark.parametrize("w,h", [(1440, 900), (768, 1024), (390, 844)])
def test_owner_console_headings_are_not_covered_with_agents_listed(server, browser, w, h):
    """Review 5 reported 'Agents' and 'Schedule of selected agent' covered. Not reproduced here (Linux Chrome); this guards it
    with two agents in the grid, the headings anywhere from the top band to mid-screen."""
    pg = _open(browser, server, w, h)
    pg.evaluate("document.getElementById('ownerhint').open = true"); pg.wait_for_timeout(300)
    pg.fill("#tok", "e2e-admin-token-not-secret"); pg.dispatch_event("#tok", "change"); pg.wait_for_timeout(600)
    for _ in range(2):
        pg.click("text=Create agent"); pg.wait_for_timeout(900)
    pg.wait_for_function("document.querySelectorAll('#agents .ag-row').length >= 2", timeout=10000)
    for name in ("Agents", "Schedule of selected agent"):
        y0 = pg.evaluate(f"[...document.querySelectorAll('#ownerhint h3')].find(e => e.textContent.trim() === '{name}').getBoundingClientRect().top + scrollY")
        for off in (h // 2, 60, 20):                   # each step scrolls down: the header slides away, as when reading
            pg.evaluate(f"scrollTo(0, {y0 - off})"); pg.wait_for_timeout(350)
            top, covered = pg.evaluate(_COVERED, ["#ownerhint h3", name])
            assert covered == [], (name, top, covered)
    pg.close()



# ---------------- header: sticky, hides on scroll down, returns on scroll up ----------------
_NAV = """() => { const n = document.querySelector('.nav'), r = n.getBoundingClientRect(), s = getComputedStyle(n);
  return {top: r.top, bottom: r.bottom, h: n.offsetHeight, hidden: n.classList.contains('nav-hidden'), pos: s.position, stop: s.top,
          tp: s.transitionProperty, td: s.transitionDuration, tf: s.transitionTimingFunction, shadow: s.boxShadow,
          blur: s.backdropFilter || s.webkitBackdropFilter || 'none', bb: s.borderBottomStyle, bg: s.backgroundColor,
          pagebg: getComputedStyle(document.body).backgroundColor, y: scrollY}; }"""


# A scroll position (in the lower part of the page) where no heading would sit in the header's band: scrolling up to
# it must bring the header back. Over a heading the header waits (see the guard tests below).
_CLEAR = """nh => { document.documentElement.style.scrollBehavior = 'auto';
  const hs = [...document.querySelectorAll('h1,h2,h3,h4')].filter(e => !e.closest('.nav')).map(e => { const r = e.getBoundingClientRect();
    return [r.top + scrollY, r.bottom + scrollY]; }).filter(([a, b]) => b > a);
  for (let y = 1500; y < document.documentElement.scrollHeight - innerHeight - 700; y += 40)
    if (!hs.some(([a, b]) => b > y - 8 && a < y + nh + 8)) return y; return null; }"""


def _up_to_clear_spot(pg, wait=450):
    """Scroll down past a clear spot, then up to it: returns the spot."""
    y = pg.evaluate(_CLEAR, pg.evaluate("document.querySelector('.nav').offsetHeight")); assert y
    pg.evaluate(f"scrollTo(0, {y + 500})"); pg.wait_for_timeout(wait)
    pg.evaluate(f"scrollTo(0, {y})"); pg.wait_for_timeout(wait)
    return y


def _wheel(pg, dy, steps=1, wait=400):
    for _ in range(steps):
        pg.mouse.wheel(0, dy); pg.wait_for_timeout(120)
    pg.wait_for_timeout(wait)


def test_header_is_sticky_and_hides_on_scroll_down_and_returns_on_scroll_up(server, browser):
    pg = _open(browser, server, 1440, 900)
    pg.mouse.move(700, 600)
    n = pg.evaluate(_NAV)
    assert n["pos"] == "sticky" and n["stop"] == "0px" and not n["hidden"] and n["top"] == 0
    _wheel(pg, 300, 4)                                         # down, well past the header height
    n = pg.evaluate(_NAV)
    assert n["y"] > n["h"] and n["hidden"] and n["bottom"] <= 0.5, n
    _up_to_clear_spot(pg)                                      # up: back, at the top of the screen
    n = pg.evaluate(_NAV)
    assert not n["hidden"] and abs(n["top"]) < 0.5, n
    pg.evaluate("scrollBy(0, 4)"); pg.wait_for_timeout(300)    # under the 8 px dead zone: no flicker
    assert not pg.evaluate(_NAV)["hidden"]
    assert pg.errs == []
    pg.close()


def test_header_is_always_shown_near_the_top_with_no_shadow_glass_or_line(server, browser):
    pg = _open(browser, server, 1440, 900)
    pg.mouse.move(700, 600)
    _wheel(pg, 40, wait=300)                                   # scrollY < header height
    n = pg.evaluate(_NAV)
    assert n["y"] < n["h"] and not n["hidden"] and n["top"] == 0, n
    assert n["shadow"] == "none" and n["blur"] == "none" and n["bb"] == "none" and n["bg"] == n["pagebg"], n
    _up_to_clear_spot(pg)                                      # mid-page, reappeared: same solid page tone
    n = pg.evaluate(_NAV)
    assert not n["hidden"] and n["bg"] == n["pagebg"] and n["shadow"] == "none" and n["blur"] == "none" and n["bb"] == "none", n
    pg.close()


def test_header_moves_by_transform_only_in_220ms_ease_out_and_keeps_its_space(server, browser):
    pg = _open(browser, server, 1440, 900)
    pg.mouse.move(700, 600)
    n = pg.evaluate(_NAV)
    assert (n["tp"], n["td"], n["tf"]) == ("transform", "0.22s", "ease-out"), n
    main_top = pg.evaluate("document.getElementById('top') ? document.querySelector('.hero').getBoundingClientRect().top + scrollY : 0")
    _wheel(pg, 300, 4)
    assert pg.evaluate("getComputedStyle(document.querySelector('.nav')).transform").startswith("matrix(1, 0, 0, 1, 0, -")
    assert pg.evaluate("document.querySelector('.hero').getBoundingClientRect().top + scrollY") == main_top   # no layout shift
    pg.close()


def test_header_hides_instantly_with_reduced_motion(server, browser):
    pg = _open(browser, server, 1440, 900, reduced_motion="reduce")
    pg.mouse.move(700, 600)
    assert pg.evaluate(_NAV)["td"] == "0s"
    _wheel(pg, 300, 4, wait=150)
    assert pg.evaluate(_NAV)["hidden"] and pg.evaluate(_NAV)["bottom"] <= 0.5
    _up_to_clear_spot(pg, wait=150)
    assert abs(pg.evaluate(_NAV)["top"]) < 0.5
    pg.close()


def test_header_stays_while_it_has_keyboard_focus_or_the_pointer(server, browser):
    pg = _open(browser, server, 1440, 900)
    pg.mouse.move(700, 600)
    _wheel(pg, 300, 4)
    assert pg.evaluate(_NAV)["hidden"]
    pg.focus(".nav-links a[href='#proof']"); pg.wait_for_timeout(400)   # Tab into it: it comes back...
    assert not pg.evaluate(_NAV)["hidden"]
    pg.evaluate("scrollBy(0, 400)"); pg.wait_for_timeout(400)          # ...and stays while focus is inside
    assert not pg.evaluate(_NAV)["hidden"]
    pg.evaluate("document.activeElement.blur()")
    pg.evaluate("scrollBy(0, 400)"); pg.wait_for_timeout(400)
    assert pg.evaluate(_NAV)["hidden"]
    _up_to_clear_spot(pg); pg.mouse.move(400, 30); pg.wait_for_timeout(200)  # pointer over it
    _wheel(pg, 300, 3)                                                   # the wheel over the header scrolls the page
    n = pg.evaluate(_NAV)
    assert n["y"] > n["h"] and not n["hidden"], n
    pg.mouse.move(700, 600); _wheel(pg, 300, 2)
    assert pg.evaluate(_NAV)["hidden"]
    pg.close()


def test_scroll_listener_is_passive_and_runs_in_animation_frames(server, browser):
    pg = browser.new_page(viewport={"width": 1440, "height": 900})
    pg.add_init_script("""(() => { window.__scroll = []; window.__raf = 0;
      const add = EventTarget.prototype.addEventListener;
      EventTarget.prototype.addEventListener = function (t, f, o) { if (t === 'scroll' && this === window) window.__scroll.push(o); return add.call(this, t, f, o); };
      const raf = window.requestAnimationFrame.bind(window); window.requestAnimationFrame = cb => { window.__raf++; return raf(cb); }; })()""")
    pg.goto(server + "/"); pg.wait_for_selector("#demo", state="visible"); pg.mouse.move(700, 600)
    opts = pg.evaluate("window.__scroll")
    assert opts and all(isinstance(o, dict) and o.get("passive") is True for o in opts), opts
    before = pg.evaluate("window.__raf"); _wheel(pg, 300, 3)
    assert pg.evaluate("window.__raf") > before and pg.evaluate(_NAV)["hidden"]
    pg.close()


def test_the_header_is_the_only_sticky_element(server, browser):
    pg = _open(browser, server, 1440, 900)
    # Sticky to the page, that is: the scheduler's date headers stick inside its own scroll box, not over the page.
    sticky = pg.evaluate("""[...document.querySelectorAll('*')].filter(e => { const s = getComputedStyle(e);
      if (s.position !== 'sticky' && s.position !== 'fixed') return false;
      for (let p = e.parentElement; p && p !== document.body; p = p.parentElement) { const ps = getComputedStyle(p);
        if (/(auto|scroll|hidden|clip)/.test(ps.overflowX + ps.overflowY)) return false; }
      return true; }).map(e => e.className || e.id)""")
    assert sticky in (["nav"], ["nav", "toasts"]), sticky          # the toast stack is fixed, not sticky, and only holds toasts
    assert pg.evaluate("[...document.querySelectorAll('*')].filter(e => getComputedStyle(e).position === 'sticky' && !e.closest('[data-ui]')).length") == 1
    pg.close()


@pytest.mark.parametrize("w,h", [(1440, 900), (768, 1024), (390, 844)])
def test_anchor_targets_are_not_covered_by_the_header_jumping_up_or_down(server, browser, w, h):
    pg = _open(browser, server, w, h)
    pad = pg.evaluate("parseFloat(getComputedStyle(document.documentElement).scrollPaddingTop)")
    assert pad == pg.evaluate("document.querySelector('.nav').offsetHeight")        # scroll-padding = header height
    for target, start in (("#proof", "0"), ("#how", "document.body.scrollHeight - innerHeight")):   # jump down, then up
        pg.evaluate(f"scrollTo(0, {start})"); pg.wait_for_timeout(500)
        pg.evaluate(f"location.hash = ''; location.hash = '{target}'")
        pg.wait_for_function("""() => { const y = scrollY, w = window; const ok = w.__ly === y; w.__ly = y; return ok; }""", polling=300, timeout=10000)
        pg.wait_for_timeout(400)
        top, covered = pg.evaluate(_COVERED, [target + " h2", ""])
        assert covered == [] and top >= 0, (target, top, covered)
    pg.close()


@pytest.mark.parametrize("w,h", [(1440, 900), (768, 1024), (390, 844)])
def test_scrolling_up_never_brings_the_header_back_over_a_heading(server, browser, w, h):
    """The header returns on scroll up, but waits while a heading sits in its band; it shows again once the heading
    has moved below it. Checked for every section heading, landing in the band from below."""
    pg = _open(browser, server, w, h)
    pg.evaluate("document.documentElement.style.scrollBehavior = 'auto'")
    ids = pg.eval_on_selector_all("section[id] h2", "els => els.filter(e => e.getClientRects().length).map(e => e.closest('section').id)")
    assert len(ids) >= 6
    shown = 0
    for sid in ids:
        y0 = pg.evaluate(f"document.querySelector('#{sid} h2').getBoundingClientRect().top + scrollY")
        pg.evaluate(f"scrollTo(0, {y0 + 700})"); pg.wait_for_timeout(300)
        pg.evaluate(f"scrollTo(0, {y0 - 10})"); pg.wait_for_timeout(400)    # up: heading lands 10 px from the top
        top, covered = pg.evaluate(_COVERED, [f"#{sid} h2", ""])
        assert covered == [], (sid, top, covered)
        pg.evaluate(f"scrollTo(0, {y0 - 300})"); pg.wait_for_timeout(400)   # further up: heading well below the header
        busy = pg.evaluate("""nh => [...document.querySelectorAll('h1,h2,h3,h4')].filter(e => !e.closest('.nav'))
          .some(e => { const r = e.getBoundingClientRect(); return r.height > 0 && r.bottom > 0 && r.top < nh; })""", pg.evaluate(_NAV)["h"])
        assert pg.evaluate(_NAV)["hidden"] == busy, sid           # back, unless another heading now sits in the band
        shown += not busy
    assert shown >= len(ids) // 2
    pg.close()
