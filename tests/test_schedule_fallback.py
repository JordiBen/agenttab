"""Schedule fallback (review t85u, option A): when Bryntum Scheduler can't show the timeline (CDN or integrity failure, an
error while constructing, or its "Trial expired" mask), the schedule is shown as an AG Grid list with the same data and
the same actions. Approve / Reject / Refund go through the one pAct() in app.js from both views.
Bryntum's own trial and verify logic is never touched by app code; the tests stub bryntum.com so nothing reaches it."""
import os, re, shutil, socket, struct, subprocess, sys, time, zlib
from pathlib import Path
import httpx, pytest

ROOT = Path(__file__).resolve().parent.parent
SCHED = (ROOT / "static" / "schedule.js").read_text(encoding="utf-8")
JS = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
NOTE = "Timeline view unavailable \u2014 showing the same schedule as a list."
DAY_MS = 86_400_000


# ---------- static: one code path, no trial tampering ----------
def test_schedule_js_has_the_exact_fallback_note():
    assert NOTE in SCHED


def test_approve_reject_refund_have_one_code_path_through_pact():
    """Neither view builds an approve/reject/refund request itself: schedule.js calls opts.pAct, app.js hands it pAct."""
    assert not re.search(r"/(approve-sandbox-card|approve|reject|refund)[`'\"]", SCHED), "schedule.js builds a purchase action path"
    assert len(re.findall(r"opts\.pAct\(", SCHED)) >= 2          # the timeline menu and the fallback list
    assert len(re.findall(r"\basync function pAct\(", JS)) == 1
    calls = re.findall(r"makeSchedule\(\$\('(\w+)'\),[^\n]*", JS)
    assert len(calls) == 2
    for line in re.findall(r"makeSchedule\(\$\('\w+'\),[^\n]*", JS):
        assert re.search(r"pAct: \(pid, act\) => pAct\(pid, act, (true|false)\)", line), line
    # the only place that turns an action into /api/purchases/{pid}/{act} is pAct itself
    assert len(re.findall(r"/api/purchases/\$\{pid\}/\$\{act\}", JS)) == 1


def test_app_code_never_touches_bryntum_trial_or_verify():
    """Option C is rejected: no reading or writing Bryntum's storage keys, no verify blocking, no clock changes."""
    assert "localStorage" not in SCHED
    for name, src in (("schedule.js", SCHED), ("app.js", JS)):
        for bad in ("b-scheduler-", "bryntum.com/verify", "trialStart", "Date.now =", "demoMode"):
            assert bad not in src, (name, bad)
    assert [l for l in JS.splitlines() if "localStorage" in l] == [
        "localStorage.removeItem('tok');   // the admin token lives only in this tab's sessionStorage"]


# ---------- browser e2e ----------
try:
    from playwright import sync_api as pw
except ImportError:          # the static tests above still run without Playwright
    pw = None
CHROME = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
pytestmark_e2e = pytest.mark.skipif(pw is None or (not CHROME and not os.environ.get("PW_BUNDLED")),
                                    reason="Playwright or Chrome missing")


def _png(w, h):
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * w for _ in range(h))
    ch = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return b"\x89PNG\r\n\x1a\n" + ch(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + ch(b"IDAT", zlib.compress(raw)) + ch(b"IEND", b"")


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    base = f"http://127.0.0.1:{port}"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PAYPAL_", "GEMINI_", "VENDOR_", "ANTHROPIC_", "LLM_"))}
    env.update(ADMIN_TOKEN="e2e-admin-token-not-secret", PAYPAL_MODE="mock", ALLOW_INSECURE_MOCK="1", DEMO_MODE="1",
               DEMO_SESSIONS_PER_IP_HOUR="100", PUBLIC_URL=base, AGENTTAB_DB=str(tmp_path_factory.mktemp("schedfb") / "e2e.db"))
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


def _open(browser, server, *, block_cdn=False, verify_png=None, trial_start_days_ago=None, clock=False, width=1440):
    """A fresh context. bryntum.com is always intercepted (never reached): aborted, or answered with verify_png."""
    ctx = browser.new_context(viewport={"width": width, "height": 900})
    seen = {"bryntum": [], "cdn_blocked": 0}

    def route(r):
        u = r.request.url
        if re.match(r"https?://([a-z0-9-]+\.)?bryntum\.com/", u):
            seen["bryntum"].append(u)
            return r.fulfill(body=verify_png, content_type="image/png") if verify_png else r.abort()
        if block_cdn and "cdn.jsdelivr.net/npm/@bryntum/" in u:
            seen["cdn_blocked"] += 1
            return r.abort()
        return r.continue_()
    ctx.route("**/*", route)
    pg = ctx.new_page()
    pg.errs = []
    pg.on("pageerror", lambda e: pg.errs.append(str(e)))
    if clock:
        pg.clock.install()
    if trial_start_days_ago is not None:     # test-only: a browser that first saw the trial this many days ago
        pg.add_init_script(f"localStorage.setItem('b-scheduler-trial-start', String(Date.now() - {trial_start_days_ago} * {DAY_MS}))")
    pg.goto(server + "/"); pg.wait_for_selector("#demo", state="visible")
    pg.click("text=1. Start demo session")
    pg.wait_for_function("document.getElementById('dbudgets').textContent.includes('$')")
    return ctx, pg, seen


FALLBACK_ROWS = "document.querySelectorAll('#dsched .schedfb .ag-center-cols-container .ag-row').length"
MASK_VISIBLE = """[...document.querySelectorAll('.b-mask')].some(m => /trial expired/i.test(m.textContent)
  && m.getClientRects().length && getComputedStyle(m).visibility !== 'hidden')"""


def _assert_fallback(pg):
    pg.wait_for_function(f"{FALLBACK_ROWS} >= 1", timeout=20000)
    assert pg.inner_text("#dsched .schedfb-note") == NOTE
    assert pg.evaluate("document.querySelector('#dsched .b-scheduler')") is None     # Bryntum container removed
    assert not pg.evaluate(MASK_VISIBLE)
    heads = pg.evaluate("[...document.querySelectorAll('#dsched .schedfb .ag-header-cell-text')].map(h => h.innerText.trim())")
    assert heads == ["Row", "Event", "Start", "End", "Status", "Actions"], heads


def _no_hscroll(pg):
    return pg.evaluate("""() => { const v = document.querySelector('#dsched .schedfb .ag-center-cols-viewport');
      return {page: document.documentElement.scrollWidth <= innerWidth, grid: v.scrollWidth <= v.clientWidth + 1}; }""")


@pytestmark_e2e
def test_cdn_blocked_shows_the_fallback_list(browser, server):
    ctx, pg, seen = _open(browser, server, block_cdn=True)
    try:
        assert seen["cdn_blocked"] >= 1
        assert pg.evaluate("!(window.bryntum && window.bryntum.scheduler)")
        _assert_fallback(pg)
        rows = pg.evaluate("[...document.querySelectorAll('#dsched .schedfb .ag-row [col-id=\"event\"]')].map(c => c.innerText)")
        assert any("Top-up $100.00" in r for r in rows), rows
        for w in (1440, 1280):
            pg.set_viewport_size({"width": w, "height": 900}); pg.wait_for_timeout(300)
            assert _no_hscroll(pg) == {"page": True, "grid": True}, w
        assert not pg.errs, pg.errs
    finally:
        ctx.close()


@pytestmark_e2e
def test_expired_trial_mask_switches_to_the_fallback(browser, server):
    ctx, pg, seen = _open(browser, server, clock=True, trial_start_days_ago=46)
    try:
        pg.clock.run_for(65000)
        _assert_fallback(pg)
        pg.clock.run_for(5000)
        assert not pg.evaluate(MASK_VISIBLE)
        assert not pg.errs, pg.errs
    finally:
        ctx.close()


@pytestmark_e2e
def test_verify_block_reply_switches_to_the_fallback(browser, server):
    ctx, pg, seen = _open(browser, server, clock=True, verify_png=_png(2, 1))
    try:
        pg.wait_for_selector("#dsched .b-scheduler", timeout=20000)    # starts as a normal timeline
        pg.clock.run_for(65000)                                        # Bryntum's own daily check runs after 60 s
        assert any("/verify" in u for u in seen["bryntum"]), seen      # answered by the stub, never the real site
        _assert_fallback(pg)
        assert not pg.errs, pg.errs
    finally:
        ctx.close()


@pytestmark_e2e
def test_normal_load_keeps_the_bryntum_timeline(browser, server):
    ctx, pg, seen = _open(browser, server, clock=True)
    try:
        pg.wait_for_selector("#dsched .b-scheduler", timeout=20000)
        pg.wait_for_function("document.querySelectorAll('#dsched .b-grid-row').length >= 1", timeout=20000)
        pg.clock.run_for(95000)                                        # past the 60 s check and the 90 s watch
        assert pg.evaluate("document.querySelector('#dsched .schedfb')") is None
        assert NOTE not in pg.inner_text("#dsched")
        assert pg.is_visible("#dsched .b-scheduler")
        assert not pg.evaluate(MASK_VISIBLE)
        assert not pg.errs, pg.errs
    finally:
        ctx.close()


SEAT = "demoBuy('shopsuite', 'inventory-seat', '', 'Inventory software to track stock across our two warehouses', 2)"
SPY = "window.__pact = []; const _p = window.pAct; window.pAct = (...a) => { window.__pact.push(a); return _p(...a); };"


def _held_row_button(pg, label):
    sel = f"#dsched .schedfb .ag-row:has([col-id='status']:text-is('pending approval')) button:text-is('{label}')"
    pg.wait_for_selector(sel, timeout=20000)
    return sel


@pytestmark_e2e
def test_fallback_approve_and_reject_go_through_pact_in_mock_mode(browser, server):
    ctx, pg, seen = _open(browser, server, block_cdn=True)
    try:
        _assert_fallback(pg)
        pg.evaluate(SPY)
        pg.evaluate(SEAT)
        pg.wait_for_function("document.querySelector('#dverdict .stamp.hold') !== null")
        pg.click(_held_row_button(pg, "Reject"))
        pg.wait_for_function("window.__pact.length === 1")
        pid, act, owner = pg.evaluate("window.__pact[0]")
        assert (act, owner) == ("reject", False) and pid.startswith("pu_")
        pg.wait_for_function(f"[...document.querySelectorAll('#dsched .schedfb .ag-row')].some(r => r.innerText.includes('rejected'))", timeout=20000)
        pg.evaluate(SEAT)
        pg.wait_for_function("document.querySelector('#dverdict .stamp.hold') !== null")
        pg.click(_held_row_button(pg, "Approve"))
        pg.wait_for_function("window.__pact.length === 2")
        pid2, act2, owner2 = pg.evaluate("window.__pact[1]")
        assert (act2, owner2) == ("approve-sandbox-card", False) and pid2 != pid
        pg.wait_for_function("""(() => { const c = [...document.querySelectorAll('#dsched .schedfb .ag-center-cols-container [col-id="status"]')];
          return c.length > 0 && !c.some(x => x.innerText.trim() === 'pending approval'); })()""", timeout=20000)   # list redrawn
        st = pg.evaluate(f"fetch('/api/me/purchases', {{headers: {{'X-Agent-Key': sessionStorage.dkey}}}}).then(r => r.json())")
        rows = st if isinstance(st, list) else st.get("purchases", st)
        assert {r["id"]: r["status"] for r in rows}.get(pid2) == "completed", rows
        assert not pg.errs, pg.errs
    finally:
        ctx.close()
