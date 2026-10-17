"""Browser e2e: the demo explains an unavailable AI guard, and MCP is reachable from the hero and the bento.
The server's AI provider (Anthropic, the default) points at a closed local port, so the AI is genuinely unavailable (no AI calls)."""
import os, shutil, socket, subprocess, sys, time
import httpx, pytest

pw = pytest.importorskip("playwright.sync_api")
CHROME = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
pytestmark = pytest.mark.skipif(not CHROME and not os.environ.get("PW_BUNDLED"), reason="no Chrome for Playwright")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MSG = "AI guard is busy, purchase is waiting for human approval, by design"


def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    port, dead = _free_port(), _free_port()
    base = f"http://127.0.0.1:{port}"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PAYPAL_", "GEMINI_", "LLM_", "ANTHROPIC_"))}
    env.update(ADMIN_TOKEN="e2e-admin-token-not-secret", PAYPAL_MODE="mock", ALLOW_INSECURE_MOCK="1", DEMO_MODE="1",
               PUBLIC_URL=base, AGENTTAB_DB=str(tmp_path_factory.mktemp("r4") / "r4.db"),
               GUARD_PROVIDER="anthropic", ANTHROPIC_BASE_URL=f"http://127.0.0.1:{dead}", ANTHROPIC_API_KEY="not-a-real-key")
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


def test_demo_says_ai_guard_busy_and_waits_for_a_human(server, browser):
    pg = browser.new_page(viewport={"width": 1440, "height": 900})
    pg.goto(server + "/"); pg.wait_for_selector("#demo", state="visible")
    pg.click("button:has-text('1. Start demo session')")
    pg.wait_for_function("document.getElementById('dout').textContent.includes('funded')")
    pg.click("text=2. Buy a supplier mug sample")
    pg.wait_for_function("document.getElementById('dverdict').textContent.includes('AI guard busy')", timeout=30000)
    assert pg.is_visible("#dbusy") and pg.inner_text("#dbusy") == MSG
    assert MSG in pg.inner_text("#dout") and "Bought" not in pg.inner_text("#dverdict")
    key = pg.evaluate("sessionStorage.dkey")
    st = [p["status"] for p in httpx.get(server + "/api/demo/purchases", headers={"X-Agent-Key": key}).json()]
    assert st == ["pending_approval"]
    pg.click("text=5. Try a gift card")                            # a deterministic block hides the busy note again
    pg.wait_for_function("document.getElementById('dverdict').textContent.includes('Blocked')")
    assert not pg.is_visible("#dbusy")


@pytest.mark.parametrize("w,h", [(1440, 900), (390, 844)])
def test_mcp_badge_and_bento_card_lead_to_the_panel(server, browser, w, h):
    pg = browser.new_page(viewport={"width": w, "height": h}, is_mobile=w < 500)
    pg.goto(server + "/"); pg.wait_for_selector("#demo", state="visible")
    badge = pg.locator("#heromcp")
    assert badge.is_visible() and "Works with any MCP client" in badge.inner_text()
    assert pg.evaluate("document.documentElement.scrollWidth <= innerWidth")
    badge.click(); pg.wait_for_timeout(1200)
    top = pg.evaluate("document.getElementById('mcp').getBoundingClientRect().top")
    assert -5 <= top < h, top                                      # the panel is in view
    pg.evaluate("window.scrollTo(0,0)")
    pg.locator("#bentomcp").scroll_into_view_if_needed(); pg.click("#bentomcp"); pg.wait_for_timeout(1200)
    assert 0 - 5 <= pg.evaluate("document.getElementById('mcp').getBoundingClientRect().top") < h
    # in the demo, the MCP panel is the next card after the buy buttons
    assert pg.evaluate("document.getElementById('dguided').nextElementSibling.id") == "mcp"
