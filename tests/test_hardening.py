"""Hardening regression tests: concurrency, demo scoping, rate limits, fail-closed guard, SSRF, refunds, races."""
import ipaddress
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

from app import procurement as pr
from app import tools
from app.config import Settings
from app.main import create_app
from tests.test_agenttab import A, SIG, _demo_client, _wh, buy, fund, shop_agent

PURPOSE = "Evaluate supplier samples for our autumn catalog"


def _client(tmp_path, name="r.db", **kw):
    s = Settings(**({"paypal_mode": "mock", "allow_insecure_mock": True, "db_path": str(tmp_path / name),
                     "llm_base_url": "", "llm_api_key": "", "admin_token": "adm", "public_url": "http://testserver"} | kw))
    return TestClient(create_app(s, allow_private_targets=True))


# ---------- concurrent purchases can't overspend ----------
def _slow_decide(monkeypatch, delay=0.05):
    real = pr.decide

    def slow(*a, **k):
        time.sleep(delay)             # widen the window between reading the balance and writing the charge
        return real(*a, **k)
    monkeypatch.setattr(pr, "decide", slow)


def test_parallel_purchases_never_overspend_balance(tmp_path, monkeypatch):
    c = _client(tmp_path)
    ag = shop_agent(c, daily_cap_usd=500.0, vendor_budgets={"samplebox": 500.0})
    fund(c, ag["agent_id"], 90.0)                         # $90 tab, 10 parallel $18 buys -> max 5
    _slow_decide(monkeypatch)
    with ThreadPoolExecutor(10) as ex:
        codes = list(ex.map(lambda _: buy(c, ag["api_key"], vendor="samplebox", item="ceramic-mug-sample").status_code,
                            range(10)))
    assert codes.count(200) == 5, codes
    assert set(codes) <= {200, 402, 403, 409}
    led = c.app.state.ledger
    assert led.balance(ag["agent_id"]) == Decimal("0")
    assert len([p for p in led.purchases(ag["agent_id"]) if p["status"] == "completed"]) == 5


def test_parallel_purchases_respect_vendor_budget_and_daily_cap(tmp_path, monkeypatch):
    c = _client(tmp_path)
    ag = shop_agent(c, vendor_budgets={"samplebox": 54.0})   # samplebox 30-day budget $54, daily cap $250
    fund(c, ag["agent_id"], 200.0)
    _slow_decide(monkeypatch)
    with ThreadPoolExecutor(10) as ex:
        codes = list(ex.map(lambda _: buy(c, ag["api_key"], vendor="samplebox", item="ceramic-mug-sample").status_code,
                            range(10)))
    assert codes.count(200) == 3, codes                   # 3 x $18 = the $54 vendor budget
    led = c.app.state.ledger
    assert led.vendor_spent_since(ag["agent_id"], "samplebox", 0) == Decimal("54.00")
    assert led.balance(ag["agent_id"]) == Decimal("146.00")


def test_commit_recheck_blocks_overspend_even_without_the_lock(tmp_path):
    """Second line of defence: the atomic commit itself refuses a charge the balance can't cover."""
    from app.ledger import Ledger
    led = Ledger(str(tmp_path / "l.db"))
    aid = led.create_agent("x", "", "p", Decimal("5"), Decimal("1"))["agent_id"]
    led.add_entry(aid, "fund", Decimal("0.30"), paypal_ref="CAP-1")
    lim = {"daily_cap": Decimal("5"), "vendor_budget": None}
    args = (aid, "samplebox", "ceramic-mug-sample", 1, Decimal("0.20"), "", PURPOSE, None, "t", "r", "{}", "tag", lim)
    assert led.commit_tab_purchase(*args)[0]
    pid, why = led.commit_tab_purchase(*args)
    assert pid is None and why == "insufficient balance"
    assert led.balance(aid) == Decimal("0.10")


# ---------- demo privileges only for demo-session agents ----------
def test_demo_mode_does_not_turn_regular_agents_into_owners(tmp_path):
    c = _demo_client(tmp_path)
    adm = {"X-Admin-Token": "x" * 32}
    ag = c.post("/api/agents", json={"name": "real", "policy": pr.SHOP_POLICY, "daily_cap_usd": 250, "per_call_cap_usd": 120,
                                     "vendor_budgets": {"samplebox": 200, "shopsuite": 300}, "approval_threshold_usd": 60},
                headers=adm).json()
    k = {"X-Agent-Key": ag["api_key"]}
    c.post(f"/api/agents/{ag['agent_id']}/topup-sandbox-card", json={"amount_usd": 100}, headers=adm)
    pid = buy(c, ag["api_key"], vendor="samplebox", item="ceramic-mug-sample").json()["purchase_id"]
    appr = buy(c, ag["api_key"], vendor="shopsuite", item="inventory-seat", quantity=2,
               purpose="Inventory software to track stock across our two warehouses").json()["purchase_id"]
    # a regular agent can't act as its own owner, even though the server is in demo mode
    assert c.post(f"/api/purchases/{pid}/refund", headers=k).status_code == 401
    assert c.post(f"/api/purchases/{appr}/approve-sandbox-card", headers=k).status_code == 401
    assert c.post(f"/api/purchases/{appr}/reject", headers=k).status_code == 401
    for method, path in (("post", "/api/demo/refund"), ("get", "/api/demo/report"), ("get", "/api/demo/ledger"),
                         ("get", "/api/demo/purchases"), ("post", "/api/demo/fast-forward?days=1")):
        assert getattr(c, method)(path, headers=k).status_code == 403, path
    # ...but "mark as used" (the agent reporting its own usage) still works
    assert c.post(f"/api/purchases/{pid}/used", headers=k).status_code == 200
    # a demo-session agent keeps its demo-owner powers
    d = {"X-Agent-Key": c.post("/api/demo/session").json()["agent_key"]}
    assert c.get("/api/demo/ledger", headers=d).status_code == 200


def test_demo_routes_closed_when_demo_mode_off(tmp_path):
    c = _client(tmp_path)
    ag = shop_agent(c)
    k = {"X-Agent-Key": ag["api_key"]}
    for method, path in (("post", "/api/demo/refund"), ("get", "/api/demo/report"), ("get", "/api/demo/ledger"),
                         ("get", "/api/demo/purchases"), ("post", "/api/demo/fast-forward?days=1"),
                         ("post", "/api/demo/session")):
        assert getattr(c, method)(path, headers=k).status_code == 404, path


def test_demo_flag_is_ignored_if_server_leaves_demo_mode(tmp_path):
    c = _demo_client(tmp_path)
    key = c.post("/api/demo/session").json()["agent_key"]
    from dataclasses import replace
    s2 = replace(c.app.state.settings, demo_mode=False)
    c2 = TestClient(create_app(s2, allow_private_targets=True))
    assert c2.get("/api/demo/ledger", headers={"X-Agent-Key": key}).status_code == 404


# ---------- one visitor can't exhaust the shared demo ----------
def test_per_agent_limits_on_topup_report_refund_purchases(tmp_path):
    c = _demo_client(tmp_path, topup_requests_per_agent_hour=2, reports_per_agent_hour=2,
                     tab_refunds_per_agent_hour=1, purchases_per_agent_hour=2, paypal_calls_per_day=100)
    k = {"X-Agent-Key": c.post("/api/demo/session").json()["agent_key"]}
    assert [c.post("/api/me/topup-request", json={"amount_usd": 1}, headers=k).status_code for _ in range(3)] == [200, 200, 429]
    assert [c.get("/api/demo/report", headers=k).status_code for _ in range(3)] == [200, 200, 429]
    assert [c.post("/api/demo/refund", headers=k).status_code for _ in range(2)] == [200, 429]
    codes = [buy(c, k["X-Agent-Key"], vendor="samplebox", item="ceramic-mug-sample").status_code for _ in range(3)]
    assert codes[-1] == 429


def test_guard_and_report_have_separate_llm_budgets(tmp_path, monkeypatch):
    import app.llm as llm
    calls = []

    def fake_chat(s, messages, **kw):
        calls.append(messages[0]["content"][:20])
        if "commentary" in messages[0]["content"]:
            return "All spending looks normal.", "m1"
        return '{"target_allowed": true, "purpose_fits_policy": true, "injection_attempt": false, "reason": "ok"}', "m1"
    monkeypatch.setattr(llm, "chat", fake_chat)
    c = _client(tmp_path, llm_api_key="k", llm_base_url="http://llm.invalid", guard_provider="gemini", llm_report_calls_per_day=1,
                llm_calls_per_day=50)
    ag = shop_agent(c)
    fund(c, ag["agent_id"], 100)
    for _ in range(3):        # report budget (1/day) is used up...
        c.get(f"/api/agents/{ag['agent_id']}/report", headers=A)
    r = buy(c, ag["api_key"], vendor="samplebox", item="ceramic-mug-sample")
    assert r.status_code == 200 and r.json()["guard"]["layer"] == "policy-ai"   # ...the guard still has its own


def test_ai_unavailable_routes_to_human_not_heuristic(tmp_path, monkeypatch):
    import app.llm as llm

    def down(*a, **k):
        raise llm.LLMError("m1: HTTP 429")
    monkeypatch.setattr(llm, "chat", down)
    c = _client(tmp_path, llm_api_key="k", llm_base_url="http://llm.invalid", guard_provider="gemini")
    ag = shop_agent(c)
    fund(c, ag["agent_id"], 100)
    r = buy(c, ag["api_key"], vendor="samplebox", item="ceramic-mug-sample")    # $18, normally auto-approved
    assert r.status_code == 202 and r.json()["guard"]["layer"] == "policy-unavailable"
    assert c.app.state.ledger.balance(ag["agent_id"]) == Decimal("100.00")      # nothing charged from the tab


def test_ai_budget_exhausted_routes_to_human(tmp_path, monkeypatch):
    import app.llm as llm
    monkeypatch.setattr(llm, "chat", lambda *a, **k: ('{"target_allowed": true, "purpose_fits_policy": true, '
                                                      '"injection_attempt": false, "reason": "ok"}', "m1"))
    c = _client(tmp_path, llm_api_key="k", llm_base_url="http://llm.invalid", guard_provider="gemini", llm_calls_per_day=1)
    ag = shop_agent(c)
    fund(c, ag["agent_id"], 100)
    assert buy(c, ag["api_key"], vendor="samplebox", item="ceramic-mug-sample").status_code == 200
    r = buy(c, ag["api_key"], vendor="samplebox", item="ceramic-mug-sample")
    assert r.status_code == 202 and "budget" in r.json()["guard"]["reason"]


def test_webhook_junk_without_headers_does_not_consume_budget(tmp_path):
    st = Settings(paypal_mode="sandbox", paypal_client_id="x", paypal_client_secret="y", paypal_webhook_id="WH1",
                  db_path=str(tmp_path / "w.db"), llm_base_url="", llm_api_key="", admin_token="adm",
                  public_url="http://testserver")
    c = TestClient(create_app(st, allow_private_targets=True))
    c.app.state.paypal.verify_webhook = lambda h, e, w: True
    for _ in range(5):
        assert _wh(c, "PAYMENT.CAPTURE.COMPLETED", {"id": "C1"}, headers={}).status_code == 400
    assert c.app.state.limiter.count("webhook:all", 86400) == 0
    assert _wh(c, "PAYMENT.CAPTURE.COMPLETED", {"id": "C1"}).status_code == 200
    assert c.app.state.limiter.count("webhook:all", 86400) == 1


# ---------- SSRF ----------
@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "0.0.0.0", "::", "::1", "::ffff:127.0.0.1",
                                "::ffff:169.254.169.254", "100.64.0.1", "fd00::1", "fe80::1", "224.0.0.1", "2002:7f00:1::1"])
def test_ip_is_public_rejects_internal_forms(ip):
    assert not tools.ip_is_public(ipaddress.ip_address(ip))


def test_ip_is_public_accepts_public():
    assert tools.ip_is_public(ipaddress.ip_address("93.184.215.14"))
    assert tools.ip_is_public(ipaddress.ip_address("2606:4700::6810:84e5"))


def test_literal_mapped_and_unspecified_targets_blocked():
    for t in ("http://[::ffff:127.0.0.1]/", "http://0.0.0.0/", "http://[::]/", "http://100.64.1.1/"):
        assert pr.literal_unsafe(t), t


class _Redirect(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/big":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            try:
                self.wfile.write(b"<p>" + b"x" * 3_000_000 + b"</p>")
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        if self.path.startswith("/loop"):
            n = int(self.path[5:] or 0)
            self.send_response(302)
            self.send_header("Location", f"/loop{n + 1}")
            self.end_headers()
            return
        self.send_response(302)
        self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
        self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture()
def redirect_site(monkeypatch):
    srv = HTTPServer(("127.0.0.1", 0), _Redirect)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    # treat the loopback test server as "public" so only the redirect target is judged
    real = tools.ip_is_public
    monkeypatch.setattr(tools, "ip_is_public", lambda ip: str(ip) == "127.0.0.1" or real(ip))
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_redirect_to_metadata_ip_is_blocked(redirect_site):
    with pytest.raises(tools.UnsafeTarget):
        tools.fetch(redirect_site + "/r")


def test_redirect_hops_are_capped(redirect_site):
    with pytest.raises(tools.UnsafeTarget, match="redirects"):
        tools.fetch(redirect_site + "/loop0")


def test_body_is_capped_at_2mb(redirect_site):
    text, r = tools.fetch(redirect_site + "/big")
    assert len(text) <= tools.MAX_BYTES


def test_connects_to_the_validated_ip(monkeypatch):
    """DNS is resolved once; the request goes to that IP with the original Host header (no rebinding)."""
    seen = {}
    monkeypatch.setattr(tools, "_resolve", lambda host: ["93.184.215.14"])

    def fake_send(self, request, **kw):
        seen["url"], seen["host"] = str(request.url), request.headers["host"]
        seen["sni"] = request.extensions.get("sni_hostname")
        import httpx
        return httpx.Response(200, html="<p>ok</p>", request=request)
    monkeypatch.setattr("httpx.Client.send", fake_send)
    tools.fetch("https://shop.example.com/page")
    assert seen == {"url": "https://93.184.215.14/page", "host": "shop.example.com", "sni": "shop.example.com"}


# ---------- tab refunds never re-refund a capture ----------
def test_tab_refund_tracks_refunded_amount_per_capture(tmp_path):
    c = _client(tmp_path)
    ag = shop_agent(c)
    aid, k = ag["agent_id"], ag["api_key"]
    fund(c, aid, 100.0)                                 # capture A (older)
    time.sleep(0.01)
    fund(c, aid, 100.0)                                 # capture B (newer)
    pid = buy(c, k, vendor="samplebox", item="ceramic-mug-sample").json()["purchase_id"]     # balance 182.00
    pp = c.app.state.paypal
    r = c.post(f"/api/agents/{aid}/refund", headers=A).json()
    assert r["refunded_usd"] == 182.0 and [x["amount_usd"] for x in r["refunds"]] == [100.0, 82.0]
    capA, capB = r["refunds"][1]["capture"], r["refunds"][0]["capture"]
    # the purchase is refunded to the tab -> $18 credit. B is fully refunded, so it must come from A.
    c.post(f"/api/purchases/{pid}/refund", headers=A)
    r = c.post(f"/api/agents/{aid}/refund", headers=A).json()
    assert r["refunds"] == [{"capture": capA, "refund": r["refunds"][0]["refund"], "amount_usd": 18.0}]
    led = c.app.state.ledger
    assert led.refunded_on_capture(capA) == Decimal("100.00") and led.refunded_on_capture(capB) == Decimal("100.00")
    assert sum(a for cap, a in pp.mock_refund_log if cap == capB) == Decimal("100.00")
    assert c.post(f"/api/agents/{aid}/refund", headers=A).json()["refunds"] == []


# ---------- return URL / webhook / card races ----------
def test_return_url_and_webhook_race_captures_once(tmp_path, monkeypatch):
    c = _client(tmp_path)
    ag = shop_agent(c)
    o = c.post(f"/api/agents/{ag['agent_id']}/topup", json={"amount_usd": 2}, headers=A).json()
    c.get(f"/mock-paypal/approve/{o['order_id']}")
    pp = c.app.state.paypal
    real = pp.capture_order

    def slow_capture(oid):
        time.sleep(0.2)
        return real(oid)
    pp.capture_order = slow_capture

    def ret(_):
        return c.get(f"/paypal/return?token={o['order_id']}", follow_redirects=False)

    def hook(_):
        return _wh(c, "CHECKOUT.ORDER.APPROVED", {"id": o["order_id"]})
    with ThreadPoolExecutor(6) as ex:
        res = list(ex.map(lambda i: (ret if i % 2 else hook)(i), range(6)))
    assert pp.mock_capture_count == 1
    assert c.app.state.ledger.balance(ag["agent_id"]) == Decimal("2.00")
    for r in res[1::2]:
        assert r.status_code in (302, 307) and "funded=" in r.headers["location"]


def test_capture_error_redirects_instead_of_raw_402(tmp_path):
    c = _client(tmp_path)
    ag = shop_agent(c)
    o = c.post(f"/api/agents/{ag['agent_id']}/topup", json={"amount_usd": 2}, headers=A).json()
    r = c.get(f"/paypal/return?token={o['order_id']}", follow_redirects=False)     # not approved yet
    assert r.status_code in (302, 307) and "payment_error" in r.headers["location"]
    assert c.app.state.ledger.order(o["order_id"])["status"] == "CREATED"            # released for a retry
    c.get(f"/mock-paypal/approve/{o['order_id']}")
    r = c.get(f"/paypal/return?token={o['order_id']}", follow_redirects=False)
    assert "funded=" in r.headers["location"]


def test_double_card_approval_charges_once(tmp_path, monkeypatch):
    c = _client(tmp_path)
    ag = shop_agent(c)
    pid = buy(c, ag["api_key"], vendor="shopsuite", item="inventory-seat", quantity=2,
              purpose="Inventory software to track stock across our two warehouses").json()["purchase_id"]
    pp = c.app.state.paypal
    real, n = pp.card_authorize, []

    def slow(*a, **k):
        n.append(1)
        time.sleep(0.2)
        return real(*a, **k)
    pp.card_authorize = slow
    with ThreadPoolExecutor(4) as ex:
        codes = list(ex.map(lambda _: c.post(f"/api/purchases/{pid}/approve-sandbox-card", headers=A).status_code, range(4)))
    assert sorted(codes) == [200, 409, 409, 409] and len(n) == 1
    led = c.app.state.ledger
    funds = [e for e in led.entries(ag["agent_id"]) if e["kind"] == "fund"]
    assert len(funds) == 1 and led.purchase(pid)["status"] == "completed"


def test_paypal_link_after_card_payment_is_not_captured(tmp_path):
    c = _client(tmp_path)
    ag = shop_agent(c)
    j = buy(c, ag["api_key"], vendor="shopsuite", item="inventory-seat", quantity=2,
            purpose="Inventory software to track stock across our two warehouses").json()
    oid = j["approve_url"].rsplit("/", 1)[1]
    assert c.post(f"/api/purchases/{j['purchase_id']}/approve-sandbox-card", headers=A).status_code == 200
    c.get(f"/mock-paypal/approve/{oid}")
    r = c.get(f"/paypal/return?token={oid}", follow_redirects=False)
    assert "payment_error=409" in r.headers["location"]
    pp = c.app.state.paypal
    assert pp.mock_capture_count == 0 and pp.mock_authorize_count == 1   # only the card's hold, never the link


def test_webhook_event_processed_once_under_concurrency(tmp_path):
    c = _client(tmp_path)
    ag = shop_agent(c)
    fund(c, ag["agent_id"], 2)
    cap = c.app.state.ledger.captures(ag["agent_id"])[0]["paypal_ref"]
    ev = {"id": cap, "amount": {"value": "2.00"}}
    with ThreadPoolExecutor(5) as ex:
        res = list(ex.map(lambda _: _wh(c, "PAYMENT.CAPTURE.REVERSED", ev, eid="WH-SAME").json(), range(5)))
    assert sum(1 for r in res if r.get("duplicate")) == 4
    refunds = [e for e in c.app.state.ledger.entries(ag["agent_id"]) if e["kind"] == "refund"]
    assert len(refunds) == 1 and c.app.state.ledger.balance(ag["agent_id"]) == Decimal("0.00")


# ---------- smaller items ----------
def test_live_items_quantity_one_and_non_refundable(tmp_path, monkeypatch):
    from tests.test_agenttab import site as _  # noqa: F401  (fixture module import only)
    c = _client(tmp_path)
    ag = shop_agent(c)
    fund(c, ag["agent_id"], 100)
    r = buy(c, ag["api_key"], vendor="sitescope-data", item="tech_detect", quantity=3, target="https://acme.example",
            purpose="Competitor tech stack research for our pricing page")
    assert r.status_code == 422 and "quantity" in r.text
    monkeypatch.setitem(__import__("app.main", fromlist=["TOOLS"]).TOOLS, "tech_detect", lambda url, ap=False: {"ok": 1})
    monkeypatch.setattr("app.main.check_url", lambda u, allow_private=False: u)
    r = buy(c, ag["api_key"], vendor="sitescope-data", item="tech_detect", target="https://acme.example",
            purpose="Competitor tech stack research for our pricing page")
    assert r.status_code == 200 and r.json()["refundable"] is False
    assert c.post(f"/api/purchases/{r.json()['purchase_id']}/refund", headers=A).status_code == 409
    ev = c.get(f"/api/schedule?agent_id={ag['agent_id']}", headers=A).json()["events"]
    assert any(e["type"] == "delivered" for e in ev)


def test_nan_and_infinite_amounts_rejected(tmp_path):
    c = _client(tmp_path)
    for bad in ("NaN", "Infinity", "-Infinity"):
        r = c.post("/api/agents", content='{"name":"x","policy":"hello world","daily_cap_usd":%s}' % bad,
                   headers=A | {"Content-Type": "application/json"})
        assert r.status_code == 422, bad
    r = c.post("/api/agents", json={"name": "x", "policy": "hello world", "vendor_budgets": {"samplebox": -1}}, headers=A)
    assert r.status_code == 422
    r = c.post("/api/agents", json={"name": "x", "policy": "hello world", "vendor_budgets": {"samplebox": 0}}, headers=A)
    assert r.status_code == 200


def test_paypal_request_ids_are_derived_from_the_operation(monkeypatch):
    from app.paypal import PayPalClient
    pp = PayPalClient(Settings(paypal_mode="sandbox", paypal_client_id="x", paypal_client_secret="y"))
    pp._access_token = lambda: "tok"
    seen = []

    class R:
        status_code = 201

        def json(self):
            return {"id": "R1", "status": "COMPLETED"}
    monkeypatch.setattr("app.paypal.httpx.request", lambda m, u, json=None, headers=None, timeout=0: seen.append(headers["PayPal-Request-Id"]) or R())
    pp.refund_capture("CAP1", Decimal("0.20"), op_id="purchase-pu_1")
    pp.refund_capture("CAP1", Decimal("0.20"), op_id="purchase-pu_1")
    assert seen[0] == seen[1] == "refund-CAP1-purchase-pu_1"


def test_mock_mode_refused_for_public_or_demo(tmp_path):
    with pytest.raises(RuntimeError):
        create_app(Settings(paypal_mode="mock", db_path=str(tmp_path / "a.db"), admin_token="x" * 32,
                            public_url="https://agenttab.onrender.com"))
    with pytest.raises(RuntimeError):
        create_app(Settings(paypal_mode="mock", db_path=str(tmp_path / "b.db"), admin_token="x" * 32,
                            public_url="http://localhost:8000", demo_mode=True))
    create_app(Settings(paypal_mode="mock", db_path=str(tmp_path / "c.db"), admin_token="adm",
                        public_url="http://localhost:8000"))


def test_mock_approve_rejects_html(tmp_path):
    c = _client(tmp_path)
    r = c.get("/mock-paypal/approve/%3Cscript%3Ealert(1)%3C%2Fscript%3E")
    assert r.status_code == 404 and "<script>alert" not in r.text


def test_report_numbers_come_from_ledger_not_model(tmp_path, monkeypatch):
    import app.llm as llm
    monkeypatch.setattr(llm, "chat", lambda s, m, **k: ("The agent spent $999 and everything was approved.", "m1"))
    c = _client(tmp_path, llm_api_key="k", llm_base_url="http://llm.invalid", guard_provider="gemini")
    ag = shop_agent(c)
    rep = c.get(f"/api/agents/{ag['agent_id']}/report", headers=A).json()
    assert rep["report"].startswith(rep["summary"]) and "0 paid purchases for $0.00" in rep["summary"]


def test_settings_read_env_at_construction(monkeypatch, tmp_path):
    monkeypatch.setenv("TOOL_CALLS_PER_IP_HOUR", "7")
    assert Settings().tool_calls_per_ip_hour == 7
    monkeypatch.delenv("TOOL_CALLS_PER_IP_HOUR")
    assert Settings().tool_calls_per_ip_hour == 40
    import importlib
    import os
    monkeypatch.chdir(tmp_path)
    import app.main
    importlib.reload(app.main)
    assert not os.path.exists(tmp_path / ".admin_token")            # importing has no side effects


def test_true_client_ip_not_trusted(tmp_path):
    c = _client(tmp_path)
    r = c.get("/api/admin/client-ip", headers=A | {"True-Client-IP": "1.2.3.4"}).json()
    assert r["resolved"] != "1.2.3.4"
    r = c.get("/api/admin/client-ip", headers=A | {"CF-Connecting-IP": "5.6.7.8"}).json()
    assert r["resolved"] == "5.6.7.8"


def test_gauntlet_dry_run_and_rate_limit(tmp_path):
    c = _client(tmp_path, gauntlet_runs_per_ip_hour=1)
    j = c.post("/api/gauntlet").json()
    assert j["attacks_total"] == 8 and j["legit_total"] == 2 and j["paypal_calls"] == 0 and j["legit_correct"] == 2
    assert j["mode"] == "keyword" and j["unguarded_would_buy"] == 8 and j["attacks_stopped"] >= 4
    assert not c.app.state.ledger.agents()          # dry run: nothing written
    assert c.post("/api/gauntlet").status_code == 429


def test_gauntlet_ai_unavailable_is_held_not_allowed(tmp_path, monkeypatch):
    import app.llm as llm

    def down(*a, **k):
        raise llm.LLMError("quota")
    monkeypatch.setattr(llm, "chat", down)
    c = _client(tmp_path, llm_api_key="k", llm_base_url="http://llm.invalid", guard_provider="gemini")
    j = c.post("/api/gauntlet").json()
    assert j["live_ai"] and j["attacks_stopped"] == j["attacks_total"]
    assert all(r["outcome"] != "allow" for r in j["cases"] if r["attack"])


def test_static_files_are_an_allowlist(tmp_path):
    c = _client(tmp_path)
    for ok in ("/static/app.css", "/static/app.js", "/static/schedule.js", "/static/fonts/roundo/Roundo-Variable.woff2"):
        assert c.get(ok).status_code == 200, ok
    for bad in ("/static/../app/main.py", "/static/%2e%2e/app/config.py", "/static/dashboard.html", "/static/fonts/",
                "/static/..%2fapp%2fmain.py"):
        assert c.get(bad).status_code == 404, bad


def test_gauntlet_global_daily_cap(tmp_path):
    c = _client(tmp_path, gauntlet_runs_per_day=2, gauntlet_runs_per_ip_hour=10)
    codes = [c.post("/api/gauntlet", headers={"CF-Connecting-IP": f"9.9.9.{i}"}).status_code for i in range(3)]
    assert codes == [200, 200, 429]


def test_dashboard_has_no_inline_admin_token_storage(tmp_path):
    c = _client(tmp_path)
    html, js = c.get("/").text, c.get("/static/app.js").text
    assert "localStorage.tok=" not in html + js and "sessionStorage.tok" in html + js
    assert 'integrity="sha384-' in html and "@33/" not in html


def test_replay_feed_is_real_eval_data_without_side_effects(tmp_path):
    import json
    from pathlib import Path
    c = _client(tmp_path)
    j = c.get("/api/replay").json()
    res = json.loads((Path(__file__).resolve().parent.parent / "eval" / "results.json").read_text())["modes"]["ai"]["rows"]
    assert len(j["decisions"]) == len(res) == 67
    by_id = {r["id"]: r for r in res}
    for d in j["decisions"]:                       # every replayed verdict is exactly the committed one
        assert d["outcome"] == by_id[d["id"]]["got"] and d["layer"] == by_id[d["id"]]["layer"]
    assert c.app.state.ledger.entries() == [] and c.app.state.paypal.mock_capture_count == 0


def test_dashboard_assets_self_hosted_or_sri_pinned(tmp_path):
    import re
    c = _client(tmp_path)
    html = c.get("/").text
    assert c.get("/static/vendor/ag-grid-community-33.3.2.min.js").status_code == 200
    assert c.get("/static/vendor/ag-grid-LICENSE.txt").status_code == 200
    for tag in re.findall(r"<(?:script|link)\b[^>]*>", html):
        src = re.search(r'(?:src|href)="([^"]+)"', tag)
        if src and src.group(1).startswith("http"):
            assert 'integrity="sha384-' in tag and 'crossorigin="anonymous"' in tag, tag
    assert "fonts.googleapis" not in html and "unpkg.com" not in html
