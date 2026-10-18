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
        assert pg.locator("#cmppair img, #cmppair script").count() == 0
        for side in ("#cmpl", "#cmpr"):
            assert XSS in pg.inner_text(side)              # shown literally, as text
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
    if (el.matches('.cmp-sub, .cmp-why, .cmp-why *, dd, dd *, .cmp-status, label, select, input, textarea, button') && fs < 16)
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
        assert "AI unavailable, human approval" in pg.inner_text("#cmpr")
        assert compare(pg, "forbidden_vendor-01") == ("BLOCKED", "BLOCKED")
        assert "Claude not called" in pg.inner_text("#cmpr")
        assert pg.inner_text("#cmpl h4") == "Rules + keyword check"
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
