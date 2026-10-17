"""Browser e2e for the status extras: hold/decline labels, frozen badge, reconcile/unfreeze, MCP connect panel.
Runs a real uvicorn server (PayPal mock mode) and drives it with Chromium. Skips when Playwright/Chrome is missing."""
import os, shutil, socket, subprocess, sys, time
import httpx, pytest

pw = pytest.importorskip("playwright.sync_api")
CHROME = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
pytestmark = pytest.mark.skipif(not CHROME and not os.environ.get("PW_BUNDLED"), reason="no Chrome for Playwright")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADM = "e2e-admin-token-not-secret"


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    base = f"http://127.0.0.1:{port}"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PAYPAL_", "GEMINI_"))}
    env.update(ADMIN_TOKEN=ADM, PAYPAL_MODE="mock", ALLOW_INSECURE_MOCK="1", DEMO_MODE="1", PUBLIC_URL=base,
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
def page(server):
    with pw.sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROME, args=["--no-sandbox"]) if CHROME else p.chromium.launch()
        pg = b.new_page(viewport={"width": 1440, "height": 900})
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto(server + "/"); pg.wait_for_selector("#demo", state="visible")
        pg.errs = errs
        yield pg
        b.close()


def test_mcp_panel_masks_and_reveals_the_demo_key(page, server):
    assert page.is_visible("#mcpnone") and not page.is_visible("#mcpbox")
    assert page.inner_text("#mcpurl") == server + "/mcp"
    page.click("text=1. Start demo session")
    page.wait_for_function("document.getElementById('dout').textContent.includes('funded')")
    key = page.evaluate("sessionStorage.dkey")
    assert key and page.is_visible("#mcpbox")
    shown = page.inner_text("#mcpkey")
    assert key not in shown and "•" in shown and key not in page.inner_text("#mcpcfg")   # masked by default
    page.click("#mcpreveal")
    assert page.inner_text("#mcpkey") == key and '"X-Agent-Key": "' + key in page.inner_text("#mcpcfg")
    assert server + "/mcp" in page.inner_text("#mcpcfg")
    page.click("#mcpreveal"); assert key not in page.inner_text("#mcpkey")


def test_charge_declined_label_frozen_badge_and_423(page, server):
    page.click("text=4. Inventory SaaS seat")
    page.wait_for_function("document.getElementById('dverdict').textContent.includes('approval')")
    key = page.evaluate("sessionStorage.dkey")
    pid = [p for p in httpx.get(server + "/api/demo/purchases", headers={"X-Agent-Key": key}).json()
           if p["status"] == "pending_approval"][0]["id"]
    r = httpx.post(f"{server}/api/purchases/{pid}/approve-sandbox-card",
                   headers={"X-Admin-Token": ADM, "X-Simulate-Decline": "TRANSACTION_REFUSED"})
    assert r.status_code in (200, 402), r.text
    page.wait_for_function("document.getElementById('dpurch').innerText.includes('charge declined')", timeout=15000)
    tip = page.get_attribute("#dpurch .pill.bad:has-text('charge declined')", "title")
    assert "frozen" in tip
    page.wait_for_function("document.querySelector('#dbudgets .pill.bad')?.textContent === 'frozen'", timeout=15000)
    page.click("text=2. Buy a supplier mug sample")
    page.wait_for_function("document.getElementById('dverdict').textContent.includes('Agent frozen')")


def test_schedule_redraws_declined_charge_without_waiting_for_its_poll(page, server):
    if not page.evaluate("!!(window.bryntum && window.bryntum.scheduler)"):
        pytest.skip("Bryntum Scheduler (CDN) not loaded")
    # the previous test declined the seat outside this tab; the purchases poll (4 s) must redraw the timeline too
    page.wait_for_function("document.getElementById('dsched').innerText.includes('Charge declined')", timeout=7000)
    assert "Needs approval" not in page.inner_text("#dsched")


def test_owner_reconcile_and_unfreeze(page, server):
    page.evaluate("document.getElementById('ownerhint').open = true")
    page.fill("#tok", ADM); page.dispatch_event("#tok", "change")
    page.wait_for_function("document.getElementById('agents').innerText.includes('frozen')", timeout=15000)
    page.click("text=Reconcile with PayPal")
    page.wait_for_function("/matches PayPal|mismatch/.test(document.getElementById('report').innerText)", timeout=15000)
    assert "capture(s) checked" in page.inner_text("#report") and "frozen" in page.inner_text("#report")
    page.click("button:has-text('Unfreeze')")
    page.wait_for_function("!document.getElementById('agents').innerText.includes('frozen') && document.getElementById('agents').innerText.includes('active')", timeout=15000)
    page.wait_for_function("!document.querySelector('#dbudgets .pill.bad')", timeout=15000)


def test_hold_cancelled_label_and_no_page_errors(page):
    html = page.evaluate("pill('failed_voided')")
    assert "hold cancelled" in html and "nothing was charged" in html and "pill gray" in html
    assert page.errs == []
