"""Browser e2e for /docs: Swagger UI renders from our own files only, in our tokens, and the way back works."""
import os, shutil, socket, subprocess, sys, time
from urllib.parse import urlparse
import httpx, pytest

pw = pytest.importorskip("playwright.sync_api")
CHROME = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
pytestmark = pytest.mark.skipif(not CHROME and not os.environ.get("PW_BUNDLED"), reason="no Chrome for Playwright")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    base = f"http://127.0.0.1:{port}"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PAYPAL_", "GEMINI_", "LLM_", "ANTHROPIC_"))}
    env.update(ADMIN_TOKEN="e2e-admin-token-not-secret", PAYPAL_MODE="mock", ALLOW_INSECURE_MOCK="1", DEMO_MODE="1",
               PUBLIC_URL=base, AGENTTAB_DB=str(tmp_path_factory.mktemp("e2e") / "e2e.db"))
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


def _docs(browser, server, w=1440, h=900):
    pg = browser.new_page(viewport={"width": w, "height": h}, is_mobile=w < 500)
    pg.errs, pg.reqs = [], []
    pg.on("pageerror", lambda e: pg.errs.append(str(e)))
    pg.on("request", lambda r: pg.reqs.append(r.url))
    pg.goto(server + "/docs", wait_until="domcontentloaded")
    pg.wait_for_function("document.querySelectorAll('#swagger-ui .opblock').length > 10", timeout=20000)
    return pg


AUDIT = r"""() => {
  const out = [];
  const rgba = s => { const m = s.match(/rgba?\(([^)]+)\)/); if (!m) return null; const p = m[1].split(/[ ,\/]+/).filter(Boolean).map(Number);
    return [p[0], p[1], p[2], p.length > 3 ? p[3] : 1]; };
  const lum = c => { const f = v => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
    return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2]); };
  const bgOf = el => { for (let e = el; e; e = e.parentElement) { const c = rgba(getComputedStyle(e).backgroundColor); if (c && c[3] > 0) return c; } return [255,255,255,1]; };
  for (const el of document.querySelectorAll('body *')) {
    const own = [...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim());
    if (!own || !el.getClientRects().length) continue;
    const cs = getComputedStyle(el); if (cs.visibility === 'hidden') continue;
    const fs = parseFloat(cs.fontSize), name = el.tagName + '.' + String(el.className).slice(0, 40);
    if (fs < 13) out.push('text under 13px: ' + name + ' ' + fs);
    if (el.tagName === 'P' && !el.closest('[data-ui]') && fs < 16) out.push('body under 16px: ' + name + ' ' + fs);
    const fg = rgba(cs.color), bg = bgOf(el), a = lum(fg), b = lum(bg), ratio = (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
    if (ratio < 4.5) out.push('contrast ' + ratio.toFixed(2) + ': ' + name + ' "' + el.textContent.trim().slice(0, 30) + '"');
    if (!/Roundo/.test(cs.fontFamily)) out.push('font ' + cs.fontFamily.slice(0, 30) + ': ' + name);
  }
  return [...new Set(out)].slice(0, 25);
}"""


@pytest.mark.parametrize("w,h", [(1440, 900), (390, 844)])
def test_docs_renders_from_our_own_files_only_and_passes_the_design_audit(browser, server, w, h):
    pg = _docs(browser, server, w, h)
    try:
        hosts = {urlparse(u).netloc for u in pg.reqs if u.startswith("http")}
        assert hosts == {urlparse(server).netloc}, hosts
        pg.click("#swagger-ui .opblock-get .opblock-summary >> nth=0"); pg.wait_for_timeout(500)   # one operation open
        assert pg.evaluate(AUDIT) == []
        assert pg.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        assert pg.errs == []
    finally:
        pg.close()


def test_docs_uses_our_tokens_and_hides_the_swagger_topbar(browser, server):
    pg = _docs(browser, server)
    try:
        st = pg.evaluate("""() => { const g = s => getComputedStyle(document.querySelector(s));
          const css = getComputedStyle(document.documentElement), t = n => css.getPropertyValue(n).trim();
          const hex = h => 'rgb(' + [1, 3, 5].map(i => parseInt(h.slice(i, i + 2), 16)).join(', ') + ')';
          return {bg: g('body').backgroundColor, want_bg: hex(t('--bg')), ink: g('.docs-head h1').color, want_ink: hex(t('--ink')),
            get: g('.opblock-get .opblock-summary-method').backgroundColor, want_get: hex(t('--surface')),
            post: g('.opblock-post .opblock-summary-method').backgroundColor, want_post: hex(t('--accent')),
            radius: g('.opblock').borderTopLeftRadius, want_radius: t('--radius'),
            font: g('.opblock-summary-path').fontFamily, topbar: document.querySelector('.swagger-ui .topbar') ?
              getComputedStyle(document.querySelector('.swagger-ui .topbar')).display : 'none'}; }""")
        assert st["bg"] == st["want_bg"] and st["ink"] == st["want_ink"], st
        assert st["get"] == st["want_get"] and st["post"] == st["want_post"], st      # not Swagger's blue/green
        assert st["get"] not in ("rgb(97, 175, 254)",) and st["post"] not in ("rgb(73, 204, 144)",)
        assert st["radius"] == st["want_radius"] and "Roundo" in st["font"] and st["topbar"] == "none", st
    finally:
        pg.close()


def test_back_link_returns_to_the_site_in_the_same_tab(browser, server):
    pg = _docs(browser, server)
    try:
        assert pg.title() == "AgentTab API reference"
        assert pg.is_visible("#docsback") and pg.inner_text("#docsback") == "← Back to AgentTab"
        pg.click("#docsback"); pg.wait_for_url(server + "/")
        assert pg.title().startswith("AgentTab")
        pg.click("#developers a[href='/docs']") if pg.locator("#developers a[href='/docs']").is_visible() else \
            (pg.locator("#developers a[href='/docs']").scroll_into_view_if_needed(), pg.click("#developers a[href='/docs']"))
        pg.wait_for_url(server + "/docs")
        assert len(browser.contexts[-1].pages) == 1                                  # same tab, no new one
    finally:
        pg.close()
