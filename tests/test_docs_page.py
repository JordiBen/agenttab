"""Our own API reference at /docs: self-hosted, pinned Swagger UI (no CDN), the site's header and a way back, our
favicon and title, and an OpenAPI document that says what it is. FastAPI's default /docs and /redoc pages are gone."""
import base64
import json
import shutil
import subprocess
import hashlib
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

ROOT = Path(__file__).resolve().parent.parent
VENDOR = "vendor/swagger-ui-5.33.3/"


def _client(tmp_path):
    return TestClient(create_app(Settings(paypal_mode="mock", admin_token="test-admin-token-long-enough-xyz",
                                          db_path=str(tmp_path / "d.db"), guard_provider="none")))


def _attrs(html):
    return re.findall(r'\b(?:src|href)="([^"]+)"', html)


def test_docs_is_our_page_with_the_site_header_and_a_way_back(tmp_path):
    r = _client(tmp_path).get("/docs")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    h = r.text
    assert "<title>AgentTab API reference</title>" in h
    assert re.search(r'<a class="logo" href="/"[^>]*>AgentTab</a>', h)
    assert re.search(r'<a class="docs-back" id="docsback" href="/">← Back to AgentTab</a>', h)
    assert "<h1>API reference</h1>" in h and "<p>The same endpoints the demo and the MCP tools use. Sandbox only.</p>" in h
    icon = re.search(r'<link rel="icon" href="([^"]+)">', h).group(1)
    assert icon == re.search(r'<link rel="icon" href="([^"]+)">', (ROOT / "static/dashboard.html").read_text(encoding="utf-8")).group(1)
    assert "fastapi" not in h.lower() and "favicon" not in h.lower()          # no FastAPI icon


def test_docs_makes_no_outside_requests_and_pins_its_assets_with_sri(tmp_path):
    c = _client(tmp_path)
    h = c.get("/docs").text
    refs = [a for a in _attrs(h) if not a.startswith("data:")]
    assert refs and not [a for a in refs if re.match(r"(https?:)?//", a)], refs
    for bad in ("cdn.", "jsdelivr", "unpkg", "swagger-ui-dist@"):
        assert bad not in h
    for path in (VENDOR + "swagger-ui-bundle.js", VENDOR + "swagger-ui.css"):
        tag = re.search(r'<(?:script|link)[^>]+(?:src|href)="/static/' + re.escape(path) + r'"[^>]*>', h).group(0)
        sri = re.search(r'integrity="(sha384-[^"]+)"', tag).group(1)
        body = c.get("/static/" + path).content
        assert sri == "sha384-" + base64.b64encode(hashlib.sha384(body).digest()).decode()
    js = (ROOT / "static/docs.js").read_text(encoding="utf-8")
    assert "url: '/openapi.json'" in js and "validatorUrl: null" in js and "http" not in js


def test_docs_assets_come_from_the_static_allowlist_with_the_apache_license(tmp_path):
    c = _client(tmp_path)
    for a in _attrs(c.get("/docs").text):
        if a.startswith("/static/"):
            r = c.get(a)
            assert r.status_code == 200, a
    lic = c.get("/static/" + VENDOR + "LICENSE")
    assert lic.status_code == 200 and "Apache License" in lic.text and "Version 2.0" in lic.text
    assert c.get("/static/" + VENDOR + "NOTICE").status_code == 200
    assert c.get("/static/" + VENDOR + "swagger-ui-bundle.js.map").status_code == 404     # allowlist only
    assert c.get("/static/" + VENDOR + "index.html").status_code == 404
    assert "5.33.3" in (ROOT / "static" / VENDOR / "swagger-ui-bundle.js").read_text(encoding="utf-8")   # the pinned build


def test_fastapi_default_docs_are_gone(tmp_path):
    c = _client(tmp_path)
    assert c.get("/redoc").status_code == 404
    assert c.get("/docs/oauth2-redirect").status_code == 404
    h = c.get("/docs").text
    assert "SwaggerUIBundle({" not in h and "swagger-ui-dist@5" not in h          # FastAPI's inline page
    assert c.app.docs_url is None and c.app.redoc_url is None


def test_openapi_has_a_title_description_and_the_repo_link(tmp_path):
    info = _client(tmp_path).get("/openapi.json").json()["info"]
    assert info["title"] == "AgentTab API"
    assert "same endpoints the demo and the MCP tools use" in info["description"] and "Sandbox only" in info["description"]
    assert info["contact"]["url"] == "https://github.com/JordiBen/agenttab"
    assert "/docs" not in _client(tmp_path).get("/openapi.json").json()["paths"]


def test_site_links_say_api_reference_and_open_in_the_same_tab():
    h = (ROOT / "static/dashboard.html").read_text(encoding="utf-8")
    links = re.findall(r'<a [^>]*href="/docs"[^>]*>([^<]*)</a>', h)
    assert links == ["API reference", "API reference"] and "API docs" not in h
    assert not re.search(r'<a [^>]*href="/docs"[^>]*target=', h)


# --- the bundle may fail to load (SRI mismatch after a CRLF checkout, a blocker, a network error) -------------------

def test_docs_js_falls_back_to_a_message_and_the_openapi_link_when_the_bundle_is_missing():
    """Static check, no browser needed: docs.js tests for SwaggerUIBundle before calling it and builds a message with
    a link to /openapi.json instead of leaving the page blank."""
    js = (ROOT / "static/docs.js").read_text(encoding="utf-8")
    guard = re.search(r"if \(typeof window\.SwaggerUIBundle !== 'function'\) \{ docsFallback\(root\); return; \}", js)
    assert guard and guard.start() < js.index("SwaggerUIBundle({")
    fb = js[js.index("function docsFallback"):js.index("window.addEventListener")]
    assert "a.href = '/openapi.json'" in fb and "didn't load" in fb and "role', 'alert'" in fb
    assert "innerHTML" not in js
    css = (ROOT / "static/docs.css").read_text(encoding="utf-8")
    assert re.search(r"\.docs-fallback\{[^}]*background:var\(--paper\)", css)


NODE = shutil.which("node")
DOM_STUB = r"""
const made = [];
function el(tag) { const e = {tagName: tag, children: [], attrs: {}, textContent: '', className: '', href: '',
  setAttribute(k, v) { this.attrs[k] = v; }, append(...xs) { for (const x of xs) this.children.push(typeof x === 'string' ? {text: x} : x); },
  replaceChildren() { this.children = []; } }; made.push(e); return e; }
const root = el('div');
let onload = null;
global.window = {addEventListener: (ev, f) => { if (ev === 'load') onload = f; }};
global.document = {getElementById: id => id === 'swagger-ui' ? root : null, createElement: el};
if (process.argv[2] === 'throws') global.window.SwaggerUIBundle = Object.assign(() => { throw new Error('x'); }, {presets: {apis: 1}});
global.SwaggerUIBundle = global.window.SwaggerUIBundle;
require(process.argv[1]);
onload();
const text = n => (n.text || n.textContent || '') + (n.children || []).map(text).join('');
const links = []; const walk = n => { if (n.tagName === 'a') links.push(n.href); (n.children || []).forEach(walk); }; walk(root);
console.log(JSON.stringify({text: text(root), links, alert: root.children[0] && root.children[0].attrs.role}));
"""


@pytest.mark.skipif(not NODE, reason="node not installed")
@pytest.mark.parametrize("mode", ["missing", "throws"])
def test_docs_js_fallback_runs_in_node_without_a_browser(mode):
    """Run the real docs.js against a tiny DOM stub: with no SwaggerUIBundle (or one that throws) the reference area
    gets the message and a /openapi.json link."""
    r = subprocess.run([NODE, "-e", DOM_STUB, str(ROOT / "static/docs.js"), mode], capture_output=True, text=True,
                       encoding="utf-8", timeout=30)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["links"] == ["/openapi.json"] and out["alert"] == "alert"
    assert "The interactive reference didn't load in this browser." in out["text"] and "/openapi.json" in out["text"]


def test_docs_css_has_no_hairline_rules_or_outlined_boxes():
    """DESIGN.md: no hairline rules, no 1px outlined boxes. The reference separates blocks with fills; the one 1px
    border left is on real form inputs (marked taste-skill-ignore, inside [data-ui])."""
    css = (ROOT / "static/docs.css").read_text(encoding="utf-8")
    body = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    for sel, decls in re.findall(r"([^{}]+)\{([^}]*)\}", body):
        if re.search(r"\bborder(?:-(?:top|bottom|left|right))?\s*:\s*(?:0?\.\d+|1)px\b", decls):
            parts = [s.strip() for s in sel.split(",")]
            assert all(re.search(r"\b(select|input|textarea)\b", s) for s in parts), sel.strip()
    assert "taste-skill-ignore" in css
    assert 'data-ui' in (ROOT / "static/docs.html").read_text(encoding="utf-8").split('id="swagger-ui"')[0][-80:]
