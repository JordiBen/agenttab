"""Browser e2e for vendor payouts (flag on, PayPal mock): the demo session is vendor-funded, the budget panel shows the
30-day budget, a buy is paid to the vendor and a refund request shows "refund with <vendor>".
Runs a real uvicorn server and Chromium. Skips when Playwright/Chrome is missing."""
import os, shutil, socket, subprocess, sys, time
import httpx, pytest

pw = pytest.importorskip("playwright.sync_api")
CHROME = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
pytestmark = pytest.mark.skipif(not CHROME and not os.environ.get("PW_BUNDLED"), reason="no Chrome for Playwright")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAKE_IDS = {"VENDOR_SAMPLEBOX_MERCHANT_ID": "SBXMERCHANT01", "VENDOR_SHOPSUITE_MERCHANT_ID": "SHOPMERCHANT2",
            "VENDOR_SITESCOPE_MERCHANT_ID": "SITEMERCHANT3"}     # fake test values, never real ids


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    base = f"http://127.0.0.1:{port}"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PAYPAL_", "GEMINI_", "VENDOR_", "ANTHROPIC_", "LLM_"))}
    env.update(ADMIN_TOKEN="e2e-admin-token-not-secret", PAYPAL_MODE="mock", ALLOW_INSECURE_MOCK="1", DEMO_MODE="1",
               PUBLIC_URL=base, VENDOR_PAYOUTS="1", AGENTTAB_DB=str(tmp_path_factory.mktemp("e2evp") / "e2e.db"), **FAKE_IDS)
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


def test_vendor_demo_budget_buy_and_refund_request(page):
    page.click("text=1. Start demo session")
    page.wait_for_function("document.getElementById('dbudgets').textContent.includes('30-day budget')")
    assert "left of $100.00" in page.inner_text("#dbudgets")
    assert "tab balance" not in page.inner_text("#dbudgets")
    page.evaluate("demoBuy('samplebox', 'linen-napkin-samples', '', 'linen napkin samples for the spring catalog')")
    page.wait_for_function("document.getElementById('dpurch').textContent.includes('Request refund')")
    assert "SampleBox" in page.inner_text("#dpurch")
    page.wait_for_function("document.getElementById('dbudgets').textContent.includes('$24.00 paid to vendors')")
    assert page.inner_text('#dbudgets').startswith('$76.00')
    page.click("#dpurch >> text=Request refund")
    page.wait_for_function("document.getElementById('dpurch').textContent.includes('refund with SampleBox')")
    assert not page.errs, page.errs
