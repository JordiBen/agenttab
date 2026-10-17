"""Browser e2e: free Render instances wipe SQLite on restart, so a demo key in sessionStorage can go stale.
On the first 401 the dashboard must forget sessionStorage.dkey and say the session expired (start a new demo).
Runs a real uvicorn server (PayPal mock mode), restarts it on an empty DB, and drives Chromium. Skips without
Playwright/Chrome."""
import os, shutil, socket, subprocess, sys, time
import httpx, pytest

pw = pytest.importorskip("playwright.sync_api")
CHROME = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
pytestmark = pytest.mark.skipif(not CHROME and not os.environ.get("PW_BUNDLED"), reason="no Chrome for Playwright")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Server:
    def __init__(self, tmp):
        s = socket.socket(); s.bind(("127.0.0.1", 0)); self.port = s.getsockname()[1]; s.close()
        self.base, self.tmp, self.p, self.n = f"http://127.0.0.1:{self.port}", tmp, None, 0

    def start(self):                     # every start uses a brand-new, empty SQLite file (like a Render restart)
        self.n += 1
        env = {k: v for k, v in os.environ.items() if not k.startswith(("PAYPAL_", "GEMINI_"))}
        env.update(ADMIN_TOKEN="e2e-admin-token-not-secret", PAYPAL_MODE="mock", ALLOW_INSECURE_MOCK="1",
                   DEMO_MODE="1", PUBLIC_URL=self.base, AGENTTAB_DB=str(self.tmp / f"wipe{self.n}.db"))
        self.p = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(self.port)],
                                  cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                if httpx.get(self.base + "/healthz", timeout=1).status_code == 200:
                    return
            except httpx.HTTPError:
                time.sleep(0.1)
        raise RuntimeError("server did not start")

    def stop(self):
        self.p.terminate(); self.p.wait(5)


@pytest.fixture
def srv(tmp_path):
    s = Server(tmp_path); s.start()
    yield s
    s.stop()


@pytest.fixture
def browser():
    with pw.sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROME, args=["--no-sandbox"]) if CHROME else p.chromium.launch()
        yield b
        b.close()


def _start_session(pg, base):
    pg.goto(base + "/"); pg.wait_for_selector("#demo", state="visible")
    pg.click("button:has-text('1. Start demo session')")
    pg.wait_for_function("document.getElementById('dout').textContent.includes('funded')")
    pg.click("text=2. Buy a supplier mug sample")
    pg.wait_for_function("document.getElementById('dverdict').textContent.includes('Bought')")
    return pg.evaluate("sessionStorage.dkey")


def _assert_expired(pg):
    pg.wait_for_function("document.getElementById('dexpired').style.display !== 'none'", timeout=15000)
    note = pg.inner_text("#dexpired").lower()
    assert "session expired" in note and "start demo session" in note
    assert pg.evaluate("sessionStorage.dkey") is None
    assert "Demo session expired" in pg.inner_text("#toasts")
    assert pg.inner_text("#dbudgets").startswith("Start a session")


def test_wiped_db_on_next_action_clears_key_and_says_expired(srv, browser):
    pg = browser.new_page(viewport={"width": 1280, "height": 900})
    errs = []; pg.on("pageerror", lambda e: errs.append(str(e)))
    key = _start_session(pg, srv.base)
    assert key
    srv.stop(); srv.start()                                   # restart on an empty DB: the key is now unknown
    assert httpx.get(srv.base + "/api/me", headers={"X-Agent-Key": key}).status_code == 401
    pg.click("text=3. Competitor research")
    _assert_expired(pg)
    pg.click("button:has-text('1. Start demo session')")                    # recovery: a new session works right away
    pg.wait_for_function("document.getElementById('dout').textContent.includes('funded')")
    assert pg.evaluate("sessionStorage.dkey") not in (None, key)
    assert pg.evaluate("document.getElementById('dexpired').style.display") == "none"
    assert errs == []


def test_stale_key_on_page_load_is_cleared_by_the_background_refresh(srv, browser):
    pg = browser.new_page(viewport={"width": 390, "height": 844})
    _start_session(pg, srv.base)
    srv.stop(); srv.start()
    pg.reload()                                               # sessionStorage survives a reload; first poll gets 401
    _assert_expired(pg)
