"""feature/landing-redesign: the page follows docs/DESIGN.md (receipt + stamp motif, olive on bone, Roundo) without
changing any number, claim or selector the app and the e2e tests rely on."""
import json
import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

ROOT = Path(__file__).parent.parent
HTML = (ROOT / "static/dashboard.html").read_text()
CSS = (ROOT / "static/app.css").read_text()
JS = (ROOT / "static/app.js").read_text()


def _client(tmp_path):
    s = Settings(paypal_mode="mock", allow_insecure_mock=True, db_path=str(tmp_path / "l.db"),
                 admin_token="adm", public_url="http://testserver", llm_base_url="", llm_api_key="")
    return TestClient(create_app(s, allow_private_targets=True))


def test_roundo_is_self_hosted_with_its_ofl_licence_next_to_it_and_inter_is_gone(tmp_path):
    c = _client(tmp_path)
    r = c.get("/static/fonts/roundo/Roundo-Variable.woff2")
    assert r.status_code == 200 and r.headers["content-type"] == "font/woff2" and r.content[:4] == b"wOF2"
    lic = c.get("/static/fonts/roundo/License/OFL.txt")
    assert lic.status_code == 200 and "SIL Open Font License" in lic.text and "itfoundry/roundo" in lic.text.lower()
    assert "font-family:Roundo" in CSS.replace(" ", "") and "/static/fonts/roundo/Roundo-Variable.woff2" in CSS
    for f in (HTML, CSS, JS):
        assert not re.search(r"\bInter\b", f) and "Nunito" not in f and "monospace" not in f
    assert c.get("/static/fonts/InterVariable.woff2").status_code == 404


def test_scorecard_api_carries_stale_modes_for_the_previous_catalog_label(tmp_path):
    j = _client(tmp_path).get("/api/scorecard").json()
    assert j["stale_modes"] == json.loads((ROOT / "eval/results.json").read_text()).get("stale_modes", [])


def test_design_tokens_and_remove_list():
    for tok in ("--bg: #ecebe7", "--paper: #f7f6f2", "--ink: #38450f", "--stamp-block: #a3261b", "--stamp-hold: #7a5200",
                "--radius: 6px", "--tooth: 12px"):
        assert tok in CSS, tok
    assert "backdrop-filter" not in CSS and "linear-gradient" not in CSS and "radial-gradient" not in CSS
    assert not re.search(r"box-shadow:\s*(?!none)", CSS), "no resting shadows"
    assert "@media (prefers-reduced-motion: reduce)" in CSS
    for gone in ('class="noise"', 'class="aurora"', 'class="gridlines"', 'class="kicker"', '"n">01<', "glass", "grad\""):
        assert gone not in HTML, gone
    assert 'name="color-scheme" content="light"' in HTML


def test_hero_receipt_primary_action_and_honesty_labels():
    hero = HTML[HTML.index('<header class="hero"'):HTML.index("</header>")]
    assert re.search(r'class="receipt[^"]*"[^>]*data-ui', hero) and 'id="feed"' in hero
    assert "Try to make the agent overspend" in hero and 'onclick="overspend()"' in hero
    assert "PayPal sandbox" in hero and "test money" in hero and 'id="herolive"' in hero
    assert "Not your card" in hero


def test_stamp_vocabulary_and_honest_replay_wording():
    for w in ("PAID", "HELD", "BLOCKED", "REFUNDED"):
        assert w in JS, w
    # eval replays are dry runs: an allowed replay line is never stamped PAID (no money moved)
    assert "dry run" in HTML.lower() or "dry run" in JS.lower()
    assert "live ? ['paid', 'PAID'] : ['pass', 'ALLOWED']" in JS


def test_sections_in_design_order_and_existing_selectors_kept():
    order = [HTML.index(s) for s in ('<header class="hero"', 'id="how"', 'id="proof"', 'id="showcase"', 'id="mcpinfo"',
                                     'id="workspace"', "<footer")]
    assert order == sorted(order)
    for sel in ('id="heromcp"', 'id="bentomcp"', 'id="dguided"', 'id="mcp"', 'id="dbusy"', 'id="dsched"', 'id="tiles"',
                'id="howbar"', 'id="stage"', 'id="scoretbl"', 'id="gbtn"'):
        assert sel in HTML, sel
    assert "by WebIntel Tools" not in HTML


def test_at_least_four_real_screenshots_are_served(tmp_path):
    c = _client(tmp_path)
    shots = re.findall(r'<img[^>]+src="(/static/img/[^"]+)"[^>]*alt="([^"]+)"', HTML)
    assert len(shots) >= 4
    for src, alt in shots:
        r = c.get(src)
        assert r.status_code == 200 and r.headers["content-type"] in ("image/webp", "image/jpeg", "image/png"), src
        assert len(alt) > 10
