"""Browser e2e for the compare panel: injected HTML in the purpose renders as text, the two receipts sit side by side
and stack on phones, the design rules hold (contrast, type size, no hairlines/gradients/emoji), AI trouble is HELD,
stamps are only ALLOWED/BLOCKED/HELD. Local uvicorn, PayPal mock, every AI/PayPal key stripped: no AI is ever called."""
import os, shutil, socket, subprocess, sys, time
import httpx, pytest

pw = pytest.importorskip("playwright.sync_api")
CHROME = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
pytestmark = pytest.mark.skipif(not CHROME and not os.environ.get("PW_BUNDLED"), reason="no Chrome for Playwright")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
XSS = '<script>window.__pwned = 1</script><img src=x onerror="window.__pwned = 2">Mug sample'


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    base = f"http://127.0.0.1:{port}"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PAYPAL_", "GEMINI_", "LLM_", "ANTHROPIC_"))}
    env.update(ADMIN_TOKEN="e2e-admin-token-not-secret", PAYPAL_MODE="mock", ALLOW_INSECURE_MOCK="1", DEMO_MODE="1",
               PUBLIC_URL=base, COMPARE_RUNS_PER_IP_HOUR="200", COMPARE_RUNS_PER_DAY="200",
               AGENTTAB_DB=str(tmp_path_factory.mktemp("e2e") / "e2e.db"))
    p = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port)], cwd=ROOT, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            if httpx.get(base + "/healthz", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.1)
    else:
        p.kill(); pytest.fail("server did not start")
    yield base
    p.terminate(); p.wait(5)


@pytest.fixture(scope="module")
def browser():
    with pw.sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROME, args=["--no-sandbox"]) if CHROME else p.chromium.launch()
        yield b
        b.close()


def open_panel(browser, server, w=1440, h=900):
    pg = browser.new_page(viewport={"width": w, "height": h}, is_mobile=w < 500)
    pg.errs = []
    pg.on("pageerror", lambda e: pg.errs.append(str(e)))
    pg.goto(server + "/")
    pg.wait_for_function("document.querySelectorAll('#cmpcase option').length > 1")
    return pg


def compare(pg, case_id=None):
    if case_id is not None:
        pg.select_option("#cmpcase", case_id)
    pg.evaluate("document.getElementById('cmpl').innerHTML = ''")
    pg.click("#cmpbtn"); pg.wait_for_selector("#cmpl .stamp"); pg.wait_for_selector("#cmpr .stamp")
    return pg.inner_text("#cmpl .stamp"), pg.inner_text("#cmpr .stamp")


def test_html_and_script_in_the_purpose_render_as_text(browser, server):
    pg = open_panel(browser, server)
    try:
        pg.select_option("#cmpcase", "")
        assert pg.is_visible("#cmpvendor")
        pg.select_option("#cmpvendor", "samplebox"); pg.select_option("#cmpitem", "ceramic-mug-sample")
        pg.fill("#cmppurpose", XSS)
        compare(pg)
        pg.wait_for_timeout(300)
        assert pg.evaluate("window.__pwned") is None
        assert pg.locator("#cmppair img, #cmppair script, #cmpbuy img, #cmpbuy script").count() == 0
        assert XSS in pg.inner_text("#cmpbuy")             # shown literally, as text, once: in "The purchase" card
        for side in ("#cmpl", "#cmpr"):
            assert "Mug sample" not in pg.inner_text(side)
        assert pg.errs == []
    finally:
        pg.close()


@pytest.mark.parametrize("w,h,stacked", [(1440, 900, False), (768, 1024, False), (390, 844, True)])
def test_receipts_side_by_side_on_desktop_and_tablet_stacked_on_phone(browser, server, w, h, stacked):
    pg = open_panel(browser, server, w, h)
    try:
        compare(pg)
        l, r = pg.locator("#cmpl").bounding_box(), pg.locator("#cmpr").bounding_box()
        if stacked:
            assert r["y"] >= l["y"] + l["height"] and abs(r["x"] - l["x"]) < 1
        else:
            assert abs(r["y"] - l["y"]) < 1 and r["x"] >= l["x"] + l["width"]
        assert pg.evaluate("document.documentElement.scrollWidth <= innerWidth")
    finally:
        pg.close()


AUDIT = r"""() => {
  const root = document.getElementById('compare'), out = [];
  const rgba = s => { const m = s.match(/rgba?\(([^)]+)\)/); if (!m) return null; const p = m[1].split(/[ ,\/]+/).filter(Boolean).map(Number);
    return [p[0], p[1], p[2], p.length > 3 ? p[3] : 1]; };
  const lum = c => { const f = v => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
    return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2]); };
  const bgOf = el => { for (let e = el; e; e = e.parentElement) { const c = rgba(getComputedStyle(e).backgroundColor); if (c && c[3] > 0) return c; } return [255,255,255,1]; };
  for (const el of [root, ...root.querySelectorAll('*')]) {
    const cs = getComputedStyle(el);
    if (/gradient/.test(cs.backgroundImage)) out.push('gradient: ' + el.className);
    for (const s of ['Top', 'Right', 'Bottom', 'Left']) {
      const w = parseFloat(cs['border' + s + 'Width']);
      if (w > 0 && cs['border' + s + 'Style'] !== 'none' && !el.closest('[data-ui]')) out.push('border outside data-ui: ' + el.tagName + '.' + el.className);
    }
    const own = [...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim()) || /^(SELECT|INPUT|TEXTAREA)$/.test(el.tagName);
    if (!own || !el.getClientRects().length) continue;
    const fs = parseFloat(cs.fontSize);
    if (fs < 13) out.push('text under 13px: ' + el.tagName + '.' + el.className + ' ' + fs);
    if (el.matches('.cmp-sub, .cmp-why, .cmp-why *, dd, dd *, .cmp-purp, .cmp-purp *, .cmp-target, .cmp-target *, .cmp-vendor, .cmp-status, label, select, input, textarea, button') && fs < 16)
      out.push('body text under 16px: ' + el.tagName + '.' + el.className + ' ' + fs);
    const fg = rgba(cs.color), bg = bgOf(el), a = lum(fg), b = lum(bg);
    const ratio = (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
    if (ratio < 4.5) out.push('contrast ' + ratio.toFixed(2) + ': ' + el.tagName + '.' + el.className);
  }
  if (/\p{Extended_Pictographic}/u.test(root.textContent)) out.push('emoji in text');
  return out;
}"""


@pytest.mark.parametrize("w,h", [(1440, 900), (390, 844)])
def test_design_audit_contrast_type_size_no_hairlines_gradients_or_emoji(browser, server, w, h):
    pg = open_panel(browser, server, w, h)
    try:
        compare(pg, "indirect_injection-05")
        assert pg.evaluate(AUDIT) == []
        pg.select_option("#cmpcase", "")
        assert pg.evaluate(AUDIT) == []                     # the custom form too
    finally:
        pg.close()


def test_ai_unavailable_is_held_and_a_rule_stop_says_claude_not_called(browser, server):
    pg = open_panel(browser, server)
    try:
        assert compare(pg, "legit-01") == ("ALLOWED", "HELD")
        assert pg.inner_text("#cmpr .cmp-why") == "Claude gave no answer, so a person decides."
        assert compare(pg, "forbidden_vendor-04") == ("BLOCKED", "BLOCKED")          # gift card: a rule stops it first
        assert pg.inner_text("#cmpr .cmp-meta") == "Claude wasn't needed: the rules decided first"
        assert pg.inner_text("#cmpl h4") == "Rules only" and pg.inner_text("#cmpr h4") == "Rules + Claude"
    finally:
        pg.close()


def test_stamps_are_only_allowed_blocked_held_never_paid(browser, server):
    pg = open_panel(browser, server)
    try:
        seen = set()
        for cid in pg.eval_on_selector_all("#cmpcase option", "os => os.map(o => o.value).filter(Boolean)"):
            seen |= set(compare(pg, cid))
        assert seen <= {"ALLOWED", "BLOCKED", "HELD"} and "PAID" not in pg.inner_text("#compare").upper()
    finally:
        pg.close()


# ---------------- review 6: the panel reads like a product, not a debug view ----------------
PRESET_LABELS = ["Instruction hidden in a web page", "Fake Facebook link", "Personal gift disguised as business", "Gift card",
                 "Social network research", "Over the budget", "Vague purpose", "Order to ignore the rules",
                 "Supplier mug sample", "Competitor tech check", "Write your own purchase…"]


def test_menu_uses_human_names_not_category_ids(browser, server):
    pg = open_panel(browser, server)
    try:
        labels = pg.eval_on_selector_all("#cmpcase option", "os => os.map(o => o.textContent)")
        assert labels == PRESET_LABELS
        assert not any(":" in l or "_" in l for l in labels)
    finally:
        pg.close()


@pytest.mark.parametrize("w,h", [(1440, 900), (390, 844)])
def test_the_purchase_is_shown_once_above_both_columns(browser, server, w, h):
    pg = open_panel(browser, server, w, h)
    try:
        compare(pg, "lookalike_unsafe-01")
        buy = pg.inner_text("#cmpbuy")
        assert pg.is_visible("#cmpbuy") and buy.startswith("The purchase\n")
        assert "Web page copy · $12.00" in buy and "SiteScope Data" in buy
        assert "“Competitor research on West Elm's social page”" in buy and "Target https://www.faceb00k.com/westelm" in buy
        assert pg.locator("#cmpbuy").bounding_box()["y"] + pg.locator("#cmpbuy").bounding_box()["height"] <= pg.locator("#cmppair").bounding_box()["y"]
        for side in ("#cmpl", "#cmpr"):                    # each column: heading, stamp, reason (and Claude's line)
            t = pg.inner_text(side)
            assert not any(x in t for x in ("SiteScope", "Web page copy", "$12.00", "faceb00k", "West Elm's social page")), t
            assert pg.locator(side + " dl, " + side + " dt").count() == 0
    finally:
        pg.close()


def test_column_wording_rules_side_and_status_line(browser, server):
    pg = open_panel(browser, server)
    try:
        assert compare(pg, "legit-12") == ("ALLOWED", "HELD")
        assert pg.inner_text("#cmpl .cmp-why") == "Nothing in the rules forbids it."
        assert pg.locator("#cmpl .cmp-meta").count() == 0
        assert pg.inner_text("#cmpstatus") == "Nothing was charged. This is a test run."
        assert compare(pg, "forbidden_vendor-04")[0] == "BLOCKED"
        assert pg.inner_text("#cmpl .cmp-why") == "Stopped by: vendor not allowed"
        text = pg.inner_text("#compare")
        for bad in ("eval label", "PayPal calls", "ledger writes", "fetches", "Keyword check only", "Claude:", "claude-",
                    " ms", "keyword check)", "heuristic"):
            assert bad not in text, bad
    finally:
        pg.close()


def test_claude_column_shows_the_reason_itself_and_seconds_or_cache_line(browser, server):
    """No AI runs in e2e, so the AI-side render is driven with the API's own shapes."""
    pg = open_panel(browser, server)
    try:
        r = pg.evaluate("""() => { const base = {verdict: 'blocked', outcome: 'block', layer: 'policy-ai', model: 'claude-haiku-5-5',
            reason: 'the link imitates facebook.com, a social network the policy forbids'};
          const html = s => { const d = document.createElement('div'); d.innerHTML = cmpReceipt(s, 'Rules + Claude', true); return d; };
          const called = html({...base, llm_called: true, cached: false, latency_ms: 1149}), cached = html({...base, llm_called: false, cached: true});
          return [called.querySelector('.cmp-why').textContent, called.querySelector('.cmp-meta').textContent,
                  cached.querySelector('.cmp-meta').textContent, called.textContent + cached.textContent,
                  cmpMeta({llm_called: true, latency_ms: 950}), cmpMeta({llm_called: true, latency_ms: 10174})]; }""")
        assert r[0] == "the link imitates facebook.com, a social network the policy forbids"      # no "Claude:" prefix
        assert r[1] == "Claude decided in 1.1 s" and r[2] == "Same answer as a recent check"
        assert "claude-haiku" not in r[3] and "ms" not in r[3].replace("forms", "")
        assert r[4] == "Claude decided in 0.9 s" and r[5] == "Claude decided in 10.2 s"
    finally:
        pg.close()
