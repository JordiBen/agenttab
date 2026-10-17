import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

HTML = b"""<html><head><title>Acme Widgets - Industrial widgets since 1999</title>
<meta name="generator" content="WordPress 6.6"><link rel="stylesheet" href="/wp-content/themes/a/style.css">
<script src="https://js.stripe.com/v3/"></script></head><body><h1>Acme</h1><p>We make widgets.</p>
<img src="a.png"><a href="/about">About</a></body></html>"""


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(HTML)

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def site():
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/"
    srv.shutdown()


@pytest.fixture()
def client(tmp_path):
    s = Settings(paypal_mode="mock", db_path=str(tmp_path / "t.db"), llm_base_url="", llm_api_key="",
                 admin_token="adm", public_url="http://testserver")
    return TestClient(create_app(s, allow_private_targets=True))


A = {"X-Admin-Token": "adm"}
POLICY = "Only buy data about competitor websites for market research. Never spend on social media sites. Never fetch facebook.com."


def new_agent(c, **kw):
    body = {"name": "r", "policy": POLICY, "daily_cap_usd": 1.0, "per_call_cap_usd": 0.10} | kw
    r = c.post("/api/agents", json=body, headers=A)
    assert r.status_code == 200
    return r.json()


def fund(c, aid, amount=5.0):
    o = c.post(f"/api/agents/{aid}/topup", json={"amount_usd": amount}, headers=A).json()
    assert o["approve_url"].endswith(o["order_id"])
    c.get(f"/mock-paypal/approve/{o['order_id']}")
    r = c.get(f"/paypal/return?token={o['order_id']}", follow_redirects=False)
    assert r.status_code in (302, 307)
    return o


def test_admin_required(client):
    assert client.post("/api/agents", json={"name": "x", "policy": "hello world"}).status_code == 401
    assert client.get("/api/ledger").status_code == 401


def test_402_then_fund_then_charge(client, site):
    ag = new_agent(client)
    k = {"X-Agent-Key": ag["api_key"]}
    r = client.post("/api/tools/tech_detect", json={"url": site, "purpose": "competitor tech research"}, headers=k)
    assert r.status_code == 402 and r.json()["topup"]["endpoint"] == "/api/me/topup-request"
    fund(client, ag["agent_id"], 5)
    assert client.get("/api/me", headers=k).json()["balance_usd"] == 5.0
    r = client.post("/api/tools/tech_detect", json={"url": site, "purpose": "competitor tech research"}, headers=k)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["charged_usd"] == 0.02 and j["balance_usd"] == 4.98
    assert "WordPress" in j["result"]["technologies"] and "Stripe" in j["result"]["technologies"]


def test_capture_is_idempotent(client):
    ag = new_agent(client)
    o = fund(client, ag["agent_id"], 3)
    client.get(f"/paypal/return?token={o['order_id']}", follow_redirects=False)
    assert client.get("/api/me", headers={"X-Agent-Key": ag["api_key"]}).json()["balance_usd"] == 3.0


def test_unapproved_order_not_credited(client):
    ag = new_agent(client)
    o = client.post(f"/api/agents/{ag['agent_id']}/topup", json={"amount_usd": 3}, headers=A).json()
    r = client.get(f"/paypal/return?token={o['order_id']}", follow_redirects=False)
    assert r.status_code == 402
    assert client.get("/api/me", headers={"X-Agent-Key": ag["api_key"]}).json()["balance_usd"] == 0.0


def test_guard_denies_policy_violation_and_logs(client, site):
    ag = new_agent(client)
    fund(client, ag["agent_id"])
    k = {"X-Agent-Key": ag["api_key"]}
    r = client.post("/api/tools/page_markdown", json={"url": "https://www.facebook.com/acme", "purpose": "competitor research on Acme"}, headers=k)
    # SSRF check passes for public host only when resolvable; either way it must not be charged
    assert r.status_code in (403, 422)
    r = client.post("/api/tools/page_markdown", json={"url": site, "purpose": "x"}, headers=k)
    assert r.status_code == 403 and r.json()["detail"]["source"] == "heuristic"
    led = client.get("/api/ledger", headers=A).json()
    assert any(e["kind"] == "deny" for e in led)
    assert client.get("/api/me", headers=k).json()["balance_usd"] == 5.0


def test_caps(client, site):
    ag = new_agent(client, daily_cap_usd=0.05, per_call_cap_usd=0.02)
    fund(client, ag["agent_id"])
    k = {"X-Agent-Key": ag["api_key"]}
    r = client.post("/api/tools/seo_audit", json={"url": site, "purpose": "competitor SEO research"}, headers=k)
    assert r.status_code == 403 and "per-call cap" in r.json()["detail"]["reason"]
    for _ in range(2):
        assert client.post("/api/tools/tech_detect", json={"url": site, "purpose": "competitor tech research"}, headers=k).status_code == 200
    r = client.post("/api/tools/tech_detect", json={"url": site, "purpose": "competitor tech research"}, headers=k)
    assert r.status_code == 403 and "daily cap" in r.json()["detail"]["reason"]


def test_refund_unused(client, site):
    ag = new_agent(client)
    fund(client, ag["agent_id"], 5)
    k = {"X-Agent-Key": ag["api_key"]}
    client.post("/api/tools/seo_audit", json={"url": site, "purpose": "competitor SEO research"}, headers=k)
    r = client.post(f"/api/agents/{ag['agent_id']}/refund", headers=A).json()
    assert r["refunded_usd"] == 4.97
    assert client.get("/api/me", headers=k).json()["balance_usd"] == 0.0
    rep = client.get(f"/api/agents/{ag['agent_id']}/report", headers=A).json()
    assert "1 paid calls" in rep["report"]


def test_ssrf_blocked_by_default(tmp_path):
    s = Settings(paypal_mode="mock", db_path=str(tmp_path / "s.db"), admin_token="adm", llm_base_url="", llm_api_key="")
    c = TestClient(create_app(s))
    ag = new_agent(c)
    fund(c, ag["agent_id"])
    r = c.post("/api/tools/page_markdown", json={"url": "http://169.254.169.254/latest", "purpose": "competitor research"},
               headers={"X-Agent-Key": ag["api_key"]})
    assert r.status_code == 422


def test_failed_tool_not_charged(client):
    ag = new_agent(client)
    fund(client, ag["agent_id"])
    k = {"X-Agent-Key": ag["api_key"]}
    r = client.post("/api/tools/page_markdown", json={"url": "http://127.0.0.1:1/", "purpose": "competitor research"}, headers=k)
    assert r.status_code == 502
    assert client.get("/api/me", headers=k).json()["balance_usd"] == 5.0
