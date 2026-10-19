"""Browser e2e for vendor payouts (flag on, PayPal mock): the demo session is vendor-funded, the budget panel shows the
30-day budget, a buy is paid to the vendor and a refund request shows "Refund requested" with the vendor in the Vendor column.
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


ROWS = """[...document.querySelectorAll('#dpurch .ag-center-cols-container .ag-row')].map(r => {
  const t = c => (r.querySelector(`[col-id="${c}"]`) || {innerText: ''}).innerText.trim();
  const pill = r.querySelector('[col-id="status"] .pill');
  return {time: t('ts'), item: t('item'), vendor: t('vendor'), status: t('status'), pill_title: pill ? pill.title : ''}; })"""


def _rows(page):
    return page.evaluate(ROWS)


def test_vendor_demo_budget_buy_and_refund_request(page):
    page.click("text=1. Start demo session")
    page.wait_for_function("document.getElementById('dbudgets').textContent.includes('30-day budget')")
    assert "left of $100.00" in page.inner_text("#dbudgets")
    assert "tab balance" not in page.inner_text("#dbudgets")
    page.evaluate("demoBuy('samplebox', 'linen-napkin-samples', '', 'linen napkin samples for the spring catalog')")
    page.wait_for_function("document.getElementById('dpurch').textContent.includes('Request refund')")
    assert "SampleBox" in [r["vendor"] for r in _rows(page)]
    page.wait_for_function("document.getElementById('dbudgets').textContent.includes('$24.00 paid to vendors')")
    assert page.inner_text('#dbudgets').startswith('$76.00')
    page.click("#dpurch >> text=Request refund")
    page.wait_for_function("document.getElementById('dpurch').textContent.includes('Refund requested')")
    row = next(r for r in _rows(page) if "Refund requested" in r["status"])
    assert row["vendor"] == "SampleBox" and "SampleBox" in row["pill_title"]   # moved from the pill to the Vendor column
    assert not page.errs, page.errs


def test_vendor_mode_copy_in_the_browser(page):
    """Runs after the buy above: vendor-mode intro, step 1, heading, no tab refund link, PAID stamp alone, budget row."""
    assert page.inner_text("#dintro") == ("Start a session: the agent gets a $100 30-day budget, and each purchase is paid "
                                          "by PayPal straight to the vendor from the owner's saved sandbox card")
    assert page.inner_text("#dstartprice") == "$100 budget"
    assert page.inner_text("#dbudgeth") == "Budget and vendors"
    assert not page.is_visible("#drefundbtn")
    page.evaluate("demoBuy('samplebox', 'ceramic-mug-sample', '', 'Quality-check a stoneware mug from a supplier before we stock it')")
    page.wait_for_function("document.querySelector('#dverdict .stamp.paid') !== null")
    assert "Bought" not in page.inner_text("#dverdict")
    row = page.evaluate("fetch('/api/me/schedule', {headers: {'X-Agent-Key': sessionStorage.dkey}}).then(r => r.json())"
                        ".then(j => j.resources.find(r => r.kind === 'agent').detail)")
    assert row == "budget $58 left"                         # $100 - $24 napkins - $18 mug
    assert not page.errs, page.errs


def test_vendor_bars_toast_pill_and_item_names(page):
    """Runs after the two tests above (napkins refund requested, mug paid): the SampleBox bar still counts both."""
    page.wait_for_function("document.getElementById('dbudgets').textContent.includes('$42.00 / $200.00')")
    assert "$42.00 paid · $0.00 set aside" in page.inner_text("#dbudgets")
    assert "Paid · $18.00" in page.inner_text("body") and "Bought · $18.00" not in page.inner_text("body")
    page.wait_for_function("document.getElementById('dpurch').textContent.includes('Refund requested')")
    assert any(r["status"] == "Refund requested" and r["vendor"] == "SampleBox" and "SampleBox Wholesale" in r["pill_title"]
               for r in _rows(page))
    clipped = page.evaluate("""[...document.querySelectorAll('#dpurch .ag-cell .pill')].filter(p => {
        const cell = p.closest('.ag-cell'); return p.getBoundingClientRect().right > cell.getBoundingClientRect().right + 0.5; }).length""")
    assert clipped == 0
    rows = {r["item"]: r["vendor"] for r in _rows(page)}
    assert rows == {"Napkin samples": "SampleBox", "Mug sample": "SampleBox"}   # short item name; vendor in its own column
    assert not page.errs, page.errs


@pytest.mark.parametrize("width", [1440, 1280])
def test_vendor_purchases_table_fits_without_horizontal_scroll(page, width):
    """Runs after the tests above (a refund-requested row and a paid row): every column fits, the Time is not cut."""
    page.set_viewport_size({"width": width, "height": 900})
    page.wait_for_timeout(400)
    fit = page.evaluate("""(() => { const g = document.getElementById('dpurch'), v = g.querySelector('.ag-center-cols-viewport');
      const cut = [...g.querySelectorAll('.ag-center-cols-container [col-id="ts"]')].filter(c => {
        const x = c.querySelector('.ag-cell-value') || c; return x.scrollWidth > x.clientWidth + 1; }).length;
      const h = g.querySelector('.ag-header-cell[col-id="ts"]').getBoundingClientRect(), gr = g.getBoundingClientRect();
      return {sw: v.scrollWidth, cw: v.clientWidth, cut, page: document.documentElement.scrollWidth, win: innerWidth,
              hdr: h.left >= gr.left - 0.5 && h.right <= gr.right + 0.5}; })()""")
    assert fit["sw"] <= fit["cw"] + 1, fit             # no horizontal scroll inside the table
    assert fit["page"] <= fit["win"], fit               # nor on the page
    assert fit["cut"] == 0 and fit["hdr"], fit          # Time column fully visible
    rows = _rows(page)
    assert rows and all(r["vendor"] == "SampleBox" and r["time"] for r in rows)
    page.set_viewport_size({"width": 1440, "height": 900})
    assert not page.errs, page.errs


def test_vendor_held_verdict_has_one_waiting_line(page):
    """Last: a fresh session (the one above has $58 left); a $64 buy is over the $60 approval line."""
    page.evaluate("sessionStorage.clear()"); page.reload(); page.wait_for_selector("#demo", state="visible")
    page.click("text=1. Start demo session")
    page.wait_for_function("document.getElementById('dbudgets').textContent.includes('30-day budget')")
    page.evaluate("demoBuy('samplebox', 'throw-blanket-sample', '', 'two cotton throw blanket samples for the winter catalog', 2)")
    page.wait_for_function("document.querySelector('#dverdict .stamp.hold') !== null")
    v = page.inner_text("#dverdict")
    assert "Waiting for: owner approval" in v and "Needs your approval" not in v
    assert page.evaluate("document.querySelectorAll('#dverdict .pill').length") == 1
    assert not page.errs, page.errs


def test_vendor_item_shows_the_quantity(page):
    """After the held $64 buy above (2 blanket samples): the Item column says "Blanket sample ×2" (video-gaps item 6)."""
    page.wait_for_function("[...document.querySelectorAll('#dpurch [col-id=\"item\"]')].some(c => c.innerText.includes('×2'))")
    items = [r["item"] for r in _rows(page)]
    assert "Blanket sample ×2" in items, items
    page.set_viewport_size({"width": 1440, "height": 900}); page.wait_for_timeout(500)
    clipped = page.evaluate("""[...document.querySelectorAll('#dpurch .ag-center-cols-container [col-id="item"]')]
      .filter(c => { const v = c.querySelector('.ag-cell-value') || c; return v.scrollWidth > v.clientWidth + 1; }).map(c => c.innerText)""")
    assert clipped == [], clipped                       # at 1440 the "×2" is on screen, not behind an ellipsis
    assert not page.errs, page.errs


def test_schedule_vendor_rows_show_the_short_name_with_the_full_name_on_hover(page):
    """Bryntum vendor rows: "SampleBox" in the row, "SampleBox Wholesale" in the tooltip (video-gaps item 10)."""
    page.wait_for_function("[...document.querySelectorAll('#dsched .sch-res.vendor b')].length >= 3", timeout=20000)
    names = page.evaluate("[...document.querySelectorAll('#dsched .sch-res.vendor b')].map(b => b.innerText.trim())")
    assert "SampleBox" in names and "ShopSuite" in names and "SampleBox Wholesale" not in names, names
    cell = page.locator("#dsched .sch-res.vendor", has_text="SampleBox").first
    cell.scroll_into_view_if_needed(); cell.hover()
    page.wait_for_function("[...document.querySelectorAll('.b-tooltip')].some(t => t.offsetParent && t.innerText.includes('SampleBox Wholesale'))",
                           timeout=10000)
    page.mouse.move(5, 5)
    assert not page.errs, page.errs
