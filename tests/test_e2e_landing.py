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


def test_proof_tiles_come_from_results_json_with_previous_catalog_label(server, browser):
    pg = _open(browser, server, 1440, 900)
    sc = httpx.get(server + "/api/scorecard").json()
    kw, ai = sc["modes"]["keyword"], sc["modes"]["ai"]
    pg.wait_for_function("document.getElementById('tai').textContent.includes('/')")
    assert pg.inner_text("#tkw").replace(" ", "") == f"{kw['attacks_stopped']}/{kw['attacks_total']}"
    assert pg.inner_text("#tai").replace(" ", "") == f"{ai['attacks_stopped']}/{ai['attacks_total']}"
    assert pg.inner_text("#hlegit").replace(" ", "") == f"{ai['legit_correct']}/{ai['legit_total']}"
    assert pg.is_visible("#tstale") == ("ai" in sc.get("stale_modes", []))
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
        assert pg.inner_text("#stage .stamp") == word and by[case]["layer"] in pg.inner_text("#stage")


def test_primary_action_funds_a_session_then_runs_the_injected_purchase(server, browser):
    pg = _open(browser, server, 1440, 900)
    with pg.expect_request(lambda r: r.url.endswith("/api/purchases") and r.method == "POST", timeout=20000) as req:
        pg.click("text=Try to make the agent overspend")
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


@pytest.mark.parametrize("w,h", [(1440, 900), (1280, 800), (390, 844)])
def test_owner_console_open_does_not_overflow_or_overlap_headings(server, browser, w, h):
    pg = _open(browser, server, w, h)
    pg.evaluate("document.getElementById('ownerhint').open = true"); pg.wait_for_timeout(800)
    assert pg.evaluate("document.documentElement.scrollWidth <= innerWidth")
    a = pg.locator("#ownerhint h3", has_text="Agents").bounding_box(); g = pg.locator("#agents").bounding_box()
    assert a["y"] + a["height"] <= g["y"] and a["x"] >= 0 and a["x"] + a["width"] <= w


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


@pytest.mark.parametrize("w,h", [(1440, 900), (390, 844)])
def test_h1_is_at_most_two_lines(server, browser, w, h):
    pg = _open(browser, server, w, h)
    lines = pg.evaluate("""() => { const h = document.querySelector('h1'); const s = getComputedStyle(h);
      return Math.round(h.getBoundingClientRect().height / parseFloat(s.lineHeight)); }""")
    assert lines <= 2
    if w <= 640:
        assert pg.inner_text("h1").strip() == "A tab, not your card."


@pytest.mark.parametrize("w,h", [(1440, 900), (1024, 800), (768, 1024)])
def test_how_it_works_captions_have_room(server, browser, w, h):
    """Review (c): no one-word-per-line captions: each caption sits below the bar with at least ~13rem of width."""
    pg = _open(browser, server, w, h)
    widths = pg.eval_on_selector_all(".how-caps li", "els => els.map(e => e.getBoundingClientRect().width)")
    assert len(widths) == 5 and min(widths) >= 200
    assert pg.evaluate("document.querySelectorAll('#howbar p').length") == 0


def test_two_screenshots_each_half_the_page_wide(server, browser):
    pg = _open(browser, server, 1440, 900)
    pg.evaluate("document.querySelector('.shots').scrollIntoView({behavior: 'instant'})"); pg.wait_for_timeout(400)
    r = pg.evaluate("""() => { const s = document.querySelector('.shots').getBoundingClientRect();
      return [...document.querySelectorAll('.shots .shot')].map(f => f.getBoundingClientRect().width / s.width); }""")
    assert len(r) == 2 and all(x >= 0.48 for x in r)


def test_feature_list_has_no_boxes(server, browser):
    pg = _open(browser, server, 1440, 900)
    r = pg.eval_on_selector_all(".features li", """els => els.map(e => { const s = getComputedStyle(e), b = getComputedStyle(e, '::before');
      return [s.backgroundColor, s.borderTopStyle, b.content !== 'none' && parseFloat(b.width) <= 14]; })""")
    assert len(r) == 6 and all(bg in ("rgba(0, 0, 0, 0)", "transparent") and bs == "none" and marker for bg, bs, marker in r)
