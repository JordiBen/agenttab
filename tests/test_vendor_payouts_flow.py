"""Vendor payouts, capture-only (docs/vendor-payouts-impl-plan.md), step 2: the buy and approval flows over HTTP.
Mock PayPal only (it records every request body and PayPal-Request-Id); no network, no AI provider, no live site."""
from __future__ import annotations

import json
import threading
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app import procurement as pr
from app.config import Settings
from app.main import create_app

main_mod = __import__("app.main", fromlist=["TOOLS"])
A = {"X-Admin-Token": "adm"}
MID = {"samplebox": "SBXMERCHANT01", "shopsuite": "SHOPMERCHANT2", "sitescope-data": "SITEMERCHANT3"}
ENV = {"samplebox": "VENDOR_SAMPLEBOX_MERCHANT_ID", "shopsuite": "VENDOR_SHOPSUITE_MERCHANT_ID",
       "sitescope-data": "VENDOR_SITESCOPE_MERCHANT_ID"}
NAPKINS = {"vendor": "samplebox", "item": "linen-napkin-samples", "quantity": 1,
           "purpose": "linen napkin samples for the spring catalog"}                     # $24.00
BLANKETS = {"vendor": "samplebox", "item": "throw-blanket-sample", "quantity": 2,
            "purpose": "two cotton throw blanket samples for the winter catalog"}         # $64.00 > $60 threshold
MUG = {"vendor": "shopsuite", "item": "inventory-seat", "quantity": 1,
       "purpose": "inventory app seat to track stock for the shop"}                       # $39.00


@pytest.fixture
def ids(monkeypatch):
    for v, name in ENV.items():
        monkeypatch.setenv(name, MID[v])
    return MID


def settings(tmp_path, **kw):
    base = dict(paypal_mode="mock", allow_insecure_mock=True, db_path=str(tmp_path / "vp.db"), llm_base_url="",
                llm_api_key="", anthropic_api_key="", admin_token="adm", public_url="http://testserver",
                vendor_payouts=True, agent_lock_timeout_s=30.0)
    return Settings(**(base | kw))


def make(tmp_path, **kw):
    app = create_app(settings(tmp_path, **kw), allow_private_targets=True)
    return TestClient(app), app.state.paypal, app.state.ledger


@pytest.fixture
def env(tmp_path, ids):
    return make(tmp_path)


def share_paypal(pp1, pp2):
    """Two app instances (two processes) talk to the same PayPal: share the mock's vault and vendor orders."""
    pp2._mock_vault = pp1._mock_vault
    pp2.mock_vendor_orders = pp1.mock_vendor_orders


def agent(c, L, envelope=100.0, card=True, daily=250.0, **kw):
    """Vendor-funded agent: switched with the step-1 ledger call (the funding endpoint has its own tests)."""
    body = {"name": "v", "policy": pr.SHOP_POLICY, "daily_cap_usd": daily, "per_call_cap_usd": 120.0,
            "vendor_budgets": {"samplebox": 200.0, "shopsuite": 300.0}, "approval_threshold_usd": 60.0} | kw
    r = c.post("/api/agents", json=body, headers=A)
    assert r.status_code == 200, r.text
    a = r.json()
    ok, why = L.set_funding_model(a["agent_id"], "vendor", round(envelope * 100))
    assert ok, why
    if card:
        assert c.post(f"/api/agents/{a['agent_id']}/saved-card", headers=A).status_code == 200
    return a


def buy(c, a, body=NAPKINS):
    return c.post("/api/purchases", json=body, headers={"X-Agent-Key": a["api_key"]})


def pays(pp):
    return [r for r in pp.mock_requests if r["op"] == "pay_vendor"]


def no_ids_in(text: str):
    assert not any(m in text for m in MID.values())


def counted(L, aid):
    return L.vendor_counted_cents(aid, 0)


def money_entries(L, aid):
    return [e for e in L.entries(aid) if e["kind"] in ("fund", "charge", "credit", "refund", "fund_reversal")]


def live(monkeypatch, fn, item="linen-napkin-samples", vendor="samplebox"):
    """Make a vendor's fulfilment controllable from the test (the existing pattern: live + TOOLS)."""
    monkeypatch.setitem(pr.VENDORS[vendor], "fulfillment", "live")
    monkeypatch.setitem(main_mod.TOOLS, item, fn)


# ---------- 1. one capture order to the vendor ----------
def test_one_capture_order_straight_to_the_vendor(env):
    c, pp, L = env
    a = agent(c, L)
    r = buy(c, a)
    assert r.status_code == 200, r.text
    j = r.json()
    pid = j["purchase_id"]
    assert j["status"] == "completed" and j["funding"] == "vendor" and j["paid_usd"] == 24.0
    assert j["paid_to"] == "SampleBox Wholesale" and j["paypal_capture_id"] and j["paypal_order_id"]
    [req] = pays(pp)
    b = req["body"]
    assert req["request_id"] == f"at-{pid}-pay"
    assert b["intent"] == "CAPTURE"
    [u] = b["purchase_units"]
    assert u["payee"] == {"merchant_id": MID["samplebox"]}
    assert u["custom_id"] == pid and u["amount"] == {"currency_code": "USD", "value": "24.00"}
    card = b["payment_source"]["card"]
    assert card["vault_id"] == L.agent(a["agent_id"])["vault_id"]
    assert card["stored_credential"] == {"payment_initiator": "MERCHANT", "payment_type": "UNSCHEDULED",
                                         "usage": "SUBSEQUENT"}
    assert pp.mock_authorize_count == 0 and pp.mock_capture_count == 0 and pp.mock_refund_log == []
    assert money_entries(L, a["agent_id"]) == []                     # AgentTab holds no money
    pu = L.purchase(pid)
    assert (pu["status"], pu["amount_cents"], pu["paypal_capture"]) == ("completed", 2400, j["paypal_capture_id"])
    no_ids_in(r.text)


# ---------- 2. order of events ----------
def test_paypal_only_after_fulfil_and_amount_is_the_reservation(env, monkeypatch):
    c, pp, L = env
    a = agent(c, L)
    events = []
    live(monkeypatch, lambda *x, **k: events.append("fulfil") or {"ok": True})
    orig = pp.pay_vendor

    def spy(*x, **k):
        events.append("pay")
        return orig(*x, **k)
    monkeypatch.setattr(pp, "pay_vendor", spy)
    r = buy(c, a)
    assert r.status_code == 200, r.text
    assert events == ["fulfil", "pay"]
    assert pays(pp)[0]["body"]["purchase_units"][0]["amount"]["value"] == "24.00"
    assert L.purchase(r.json()["purchase_id"])["amount_cents"] == 2400


def test_reserved_before_fulfil(env, monkeypatch):
    c, pp, L = env
    a = agent(c, L)
    seen = []
    live(monkeypatch, lambda *x, **k: seen.append(counted(L, a["agent_id"])) or {"ok": True})
    assert buy(c, a).status_code == 200
    assert seen == [2400]                       # the reservation counted while the vendor was delivering


# ---------- 3. fulfil fails -> zero PayPal calls ----------
def test_fulfil_fails_no_paypal_call(env, monkeypatch):
    c, pp, L = env
    a = agent(c, L)
    live(monkeypatch, lambda *x, **k: (_ for _ in ()).throw(RuntimeError("vendor down")))
    r = buy(c, a)
    assert r.status_code == 502 and "not charged" in r.text
    assert pays(pp) == []
    [pu] = [p for p in L.purchases(a["agent_id"]) if p["status"] != "denied"]
    assert pu["status"] == "failed" and counted(L, a["agent_id"]) == 0
    assert not L.agent(a["agent_id"])["frozen_reason"]


# ---------- 4. decline after delivery -> payment_failed + freeze ----------
@pytest.mark.parametrize("issue,status", [("INSTRUMENT_DECLINED", 422), ("PAYEE_ACCOUNT_RESTRICTED", 422),
                                          ("PERMISSION_DENIED", 403), ("INVALID_REQUEST", 400)])
def test_definite_refusal_after_delivery(env, issue, status):
    c, pp, L = env
    a = agent(c, L)
    pp.mock_vendor_script = [("error", status, issue)]
    r = buy(c, a)
    assert r.status_code == 402
    d = r.json()["detail"]
    assert d["error"] == "payment_failed" and d["issue"] == issue
    pid = d["purchase_id"]
    assert L.purchase(pid)["status"] == "payment_failed"
    assert counted(L, a["agent_id"]) == 0                       # nothing was charged
    assert L.agent(a["agent_id"])["frozen_reason"] and pid in L.agent(a["agent_id"])["frozen_reason"]
    assert len(pays(pp)) == 1                                   # a definite refusal is never retried
    assert buy(c, a).status_code == 423                         # frozen: the next buy is refused
    assert len(pays(pp)) == 1
    no_ids_in(r.text)


# ---------- 7. timeout -> one retry with the same Request-Id ----------
def test_timeout_after_paypal_completed_retry_returns_first_order(env):
    c, pp, L = env
    a = agent(c, L)
    pp.mock_vendor_script = ["timeout_after"]
    r = buy(c, a)
    assert r.status_code == 200, r.text
    pid = r.json()["purchase_id"]
    reqs = pays(pp)
    assert [q["request_id"] for q in reqs] == [f"at-{pid}-pay"] * 2
    assert len(pp.mock_vendor_orders) == 1                      # PayPal made one order; the retry replayed it
    assert r.json()["paypal_capture_id"] == next(iter(pp.mock_vendor_orders.values()))["purchase_units"][0]["payments"]["captures"][0]["id"]
    assert L.purchase(pid)["status"] == "completed"


@pytest.mark.parametrize("first", ["timeout_before", ("error", 500, "INTERNAL_SERVER_ERROR"),
                                   ("error", 503, ""), ("error", 409, "DUPLICATE_REQUEST_ID"), ("error", 429, "RATE_LIMIT_REACHED")])
def test_unknown_then_ok(env, first):
    c, pp, L = env
    a = agent(c, L)
    pp.mock_vendor_script = [first]
    r = buy(c, a)
    assert r.status_code == 200, r.text
    assert len(pays(pp)) == 2 and len({q["request_id"] for q in pays(pp)}) == 1


@pytest.mark.parametrize("script", [["timeout_before", "timeout_before"],
                                    [("error", 500, ""), ("error", 502, "")],
                                    ["timeout_before", ("error", 503, "")]])
def test_still_unknown_after_one_retry(env, script):
    c, pp, L = env
    a = agent(c, L)
    pp.mock_vendor_script = list(script)
    r = buy(c, a)
    assert r.status_code == 502
    d = r.json()["detail"]
    assert d["error"] == "payment_unknown"
    assert len(pays(pp)) == 2                                    # exactly one retry
    assert L.purchase(d["purchase_id"])["status"] == "payment_unknown"
    assert counted(L, a["agent_id"]) == 2400                     # may have been charged: keeps counting
    assert L.agent(a["agent_id"])["frozen_reason"]


def test_unexpected_exception_while_paying_is_unknown_not_lost(env, monkeypatch):
    c, pp, L = env
    a = agent(c, L)
    monkeypatch.setattr(pp, "pay_vendor", lambda *x, **k: (_ for _ in ()).throw(KeyError("purchase_units")))
    r = buy(c, a)
    assert r.status_code == 502 and r.json()["detail"]["error"] == "payment_unknown"
    assert L.purchase(r.json()["detail"]["purchase_id"])["status"] == "payment_unknown"
    assert L.agent(a["agent_id"])["frozen_reason"]


# ---------- 11. response checks ----------
def _pu(j):
    return j["purchase_units"][0]


def _cap(j):
    return _pu(j)["payments"]["captures"][0]


TAMPER = {
    "wrong_payee": lambda j: _pu(j).update(payee={"merchant_id": "OTHERMERCH001"}),
    "missing_payee": lambda j: _pu(j).pop("payee"),
    "capture_payee_differs": lambda j: _cap(j).update(payee={"merchant_id": "OTHERMERCH001"}),
    "wrong_amount": lambda j: _cap(j).update(amount={"currency_code": "USD", "value": "24.01"}),
    "unit_amount_differs": lambda j: _pu(j).update(amount={"currency_code": "USD", "value": "2.40"}),
    "wrong_currency": lambda j: _cap(j).update(amount={"currency_code": "EUR", "value": "24.00"}),
    "sub_cent_amount": lambda j: _cap(j).update(amount={"currency_code": "USD", "value": "24.001"}),
    "two_captures": lambda j: _pu(j)["payments"]["captures"].append(dict(_cap(j), id="CAP-SECOND")),
    "no_capture": lambda j: _pu(j)["payments"].update(captures=[]),
    "order_not_completed": lambda j: j.update(status="PAYER_ACTION_REQUIRED"),
    "capture_declined": lambda j: _cap(j).update(status="DECLINED"),
    "custom_id_differs": lambda j: _pu(j).update(custom_id="pu_000000000000"),
    "two_units": lambda j: j["purchase_units"].append(dict(_pu(j))),
    "authorization_instead": lambda j: _pu(j)["payments"].update(authorizations=[{"id": "A1", "status": "CREATED"}]),
}


@pytest.mark.parametrize("case", ["wrong_payee", "missing_payee", "capture_payee_differs", "wrong_amount",
                                  "unit_amount_differs", "wrong_currency", "sub_cent_amount", "two_captures",
                                  "no_capture", "order_not_completed", "capture_declined", "custom_id_differs",
                                  "two_units", "authorization_instead"])
def test_response_mismatch_is_flagged_and_freezes(env, case):
    c, pp, L = env
    a = agent(c, L)
    pp.mock_vendor_tamper = TAMPER[case]
    r = buy(c, a)
    assert r.status_code == 502, r.text
    d = r.json()["detail"]
    assert d["error"] == "payment_flagged"
    pu = L.purchase(d["purchase_id"])
    assert pu["status"] == "flagged" and pu["flag"]
    assert counted(L, a["agent_id"]) == 2400
    assert L.agent(a["agent_id"])["frozen_reason"]
    assert len(pays(pp)) == 1                                    # never retried automatically
    no_ids_in(r.text)
    no_ids_in(json.dumps(L.purchases(a["agent_id"])))


def test_capture_pending_is_payment_pending_and_counts(env):
    c, pp, L = env
    a = agent(c, L)
    pp.mock_vendor_tamper = lambda j: _cap(j).update(status="PENDING")
    r = buy(c, a)
    assert r.status_code == 200 and r.json()["status"] == "payment_pending"
    pu = L.purchase(r.json()["purchase_id"])
    assert pu["status"] == "payment_pending" and pu["paypal_capture"]
    assert counted(L, a["agent_id"]) == 2400 and not L.agent(a["agent_id"])["frozen_reason"]


# ---------- 10. no id, no payment ----------
def test_vendor_without_id_blocked_before_guard_and_fulfil(env, monkeypatch):
    c, pp, L = env
    a = agent(c, L)
    monkeypatch.delenv("VENDOR_SAMPLEBOX_MERCHANT_ID")
    calls = []
    live(monkeypatch, lambda *x, **k: calls.append(1) or {})
    monkeypatch.setattr(main_mod.SpendGuard, "evaluate", lambda *x, **k: calls.append("guard"))
    r = buy(c, a)
    assert r.status_code == 403 and r.json()["detail"]["layer"] == "payee"
    assert calls == [] and pays(pp) == [] and counted(L, a["agent_id"]) == 0


def test_malformed_id_is_no_id(env, monkeypatch):
    c, pp, L = env
    a = agent(c, L)
    monkeypatch.setenv("VENDOR_SAMPLEBOX_MERCHANT_ID", "not-an-id")
    r = buy(c, a)
    assert r.status_code == 403 and r.json()["detail"]["layer"] == "payee" and pays(pp) == []


# ---------- 13. no card ----------
def test_no_card_402_card_required_no_paypal(env, monkeypatch):
    c, pp, L = env
    a = agent(c, L, card=False)
    monkeypatch.setattr(main_mod.SpendGuard, "evaluate", lambda *x, **k: pytest.fail("guard ran"))
    r = buy(c, a)
    assert r.status_code == 402
    j = r.json()
    assert j["error"] == "card_required" and j["save_card"]["endpoint"] == f"/api/agents/{a['agent_id']}/saved-card"
    assert "topup" not in j and pp.mock_requests == [] and L.purchases(a["agent_id"]) == []


# ---------- frozen ----------
def test_frozen_vendor_agent_refused_before_paypal(env):
    c, pp, L = env
    a = agent(c, L)
    L.freeze(a["agent_id"], "owner review")
    assert buy(c, a).status_code == 423 and pays(pp) == []


# ---------- 12. above the approval threshold ----------
def test_above_threshold_reserves_no_paypal(env):
    c, pp, L = env
    a = agent(c, L)
    r = buy(c, a, BLANKETS)
    assert r.status_code == 202, r.text
    j = r.json()
    assert j["status"] == "approval_required" and j["approve"]["endpoint"] == f"/api/purchases/{j['purchase_id']}/approve"
    assert "approve_url" not in j                                 # no PayPal link: nothing to pay to AgentTab
    assert pp.mock_requests == [] and pp._mock_orders == {}
    assert L.purchase(j["purchase_id"])["status"] == "pending_approval" and counted(L, a["agent_id"]) == 6400


def test_approve_then_fulfil_then_one_capture_order(env, monkeypatch):
    c, pp, L = env
    a = agent(c, L)
    pid = buy(c, a, BLANKETS).json()["purchase_id"]
    events = []
    live(monkeypatch, lambda *x, **k: events.append("fulfil") or {"ok": 1}, item="throw-blanket-sample")
    r = c.post(f"/api/purchases/{pid}/approve", headers=A)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "completed" and events == ["fulfil"]
    [req] = pays(pp)
    assert req["request_id"] == f"at-{pid}-pay" and req["body"]["purchase_units"][0]["amount"]["value"] == "64.00"
    assert L.purchase(pid)["status"] == "completed"


def test_plain_agent_key_cannot_approve(env):
    c, pp, L = env
    a = agent(c, L)
    pid = buy(c, a, BLANKETS).json()["purchase_id"]
    r = c.post(f"/api/purchases/{pid}/approve", headers={"X-Agent-Key": a["api_key"]})
    assert r.status_code == 401
    other = agent(c, L)
    assert c.post(f"/api/purchases/{pid}/approve", headers={"X-Agent-Key": other["api_key"]}).status_code == 401
    assert c.post(f"/api/purchases/{pid}/approve").status_code == 401
    assert pays(pp) == [] and L.purchase(pid)["status"] == "pending_approval"


def test_reject_no_paypal_and_stops_counting(env):
    c, pp, L = env
    a = agent(c, L)
    pid = buy(c, a, BLANKETS).json()["purchase_id"]
    assert c.post(f"/api/purchases/{pid}/reject", headers=A).status_code == 200
    assert pp.mock_requests == [] and counted(L, a["agent_id"]) == 0
    assert c.post(f"/api/purchases/{pid}/approve", headers=A).status_code == 409 and pays(pp) == []


def test_expired_approval_no_paypal(env):
    c, pp, L = env
    a = agent(c, L)
    pid = buy(c, a, BLANKETS).json()["purchase_id"]
    L.update_purchase(pid, approval_expires=time.time() - 1)
    r = c.post(f"/api/purchases/{pid}/approve", headers=A)
    assert r.status_code == 409 and "expired" in r.text
    assert pays(pp) == [] and L.purchase(pid)["status"] == "expired" and counted(L, a["agent_id"]) == 0


def test_stale_pending_approval_stops_counting_at_next_reservation(env):
    c, pp, L = env
    a = agent(c, L, envelope=70.0)
    pid = buy(c, a, BLANKETS).json()["purchase_id"]              # $64 of a $70 envelope
    assert buy(c, a).status_code == 403                           # $24 more doesn't fit
    L.update_purchase(pid, approval_expires=time.time() - 1)
    assert buy(c, a).status_code == 200
    assert L.purchase(pid)["status"] == "expired"


def test_approve_refused_when_frozen_or_no_card_or_payee_changed(env, monkeypatch):
    c, pp, L = env
    a = agent(c, L)
    pid = buy(c, a, BLANKETS).json()["purchase_id"]
    L.freeze(a["agent_id"], "x")
    assert c.post(f"/api/purchases/{pid}/approve", headers=A).status_code == 423
    L.unfreeze(a["agent_id"])
    monkeypatch.setenv("VENDOR_SAMPLEBOX_MERCHANT_ID", "NEWMERCHANT01")
    r = c.post(f"/api/purchases/{pid}/approve", headers=A)
    assert r.status_code == 409 and "changed" in r.text
    monkeypatch.setenv("VENDOR_SAMPLEBOX_MERCHANT_ID", MID["samplebox"])
    assert c.delete(f"/api/agents/{a['agent_id']}/saved-card", headers=A).status_code == 200
    assert c.post(f"/api/purchases/{pid}/approve", headers=A).json()["error"] == "card_required"
    assert pays(pp) == [] and L.purchase(pid)["status"] == "pending_approval"


def test_approval_decline_after_delivery_payment_failed(env):
    c, pp, L = env
    a = agent(c, L)
    pid = buy(c, a, BLANKETS).json()["purchase_id"]
    pp.mock_vendor_script = [("error", 422, "INSTRUMENT_DECLINED")]
    r = c.post(f"/api/purchases/{pid}/approve", headers=A)
    assert r.status_code == 402 and L.purchase(pid)["status"] == "payment_failed"
    assert L.agent(a["agent_id"])["frozen_reason"]


def test_sandbox_card_approval_refused_for_vendor_purchases(env):
    c, pp, L = env
    a = agent(c, L)
    pid = buy(c, a, BLANKETS).json()["purchase_id"]
    assert c.post(f"/api/purchases/{pid}/approve-sandbox-card", headers=A).status_code == 409
    assert pp.mock_authorize_count == 0 and pp.mock_requests == []


def test_vendor_approve_endpoint_refuses_tab_purchases(tmp_path, ids):
    c, pp, L = make(tmp_path)
    r = c.post("/api/agents", json={"name": "t", "policy": pr.SHOP_POLICY, "daily_cap_usd": 250.0,
                                    "per_call_cap_usd": 120.0, "approval_threshold_usd": 60.0}, headers=A).json()
    rr = c.post("/api/purchases", json=BLANKETS, headers={"X-Agent-Key": r["api_key"]})
    assert rr.status_code == 202
    ra = c.post(f"/api/purchases/{rr.json()['purchase_id']}/approve", headers=A)
    assert ra.status_code == 409 and "not a vendor-paid purchase" in ra.text
    assert pays(pp) == [] and L.purchase(rr.json()["purchase_id"])["status"] == "pending_approval"


def test_live_paypal_base_refuses_vendor_flow_before_anything(tmp_path, ids, monkeypatch):
    """Vendor payouts never run against live PayPal: refused before the guard, the reservation and fulfilment."""
    import httpx
    monkeypatch.setattr(httpx, "request", lambda *x, **k: pytest.fail("network call"))
    monkeypatch.setattr(httpx, "post", lambda *x, **k: pytest.fail("network call"))
    c, pp, L = make(tmp_path, paypal_mode="sandbox", paypal_base="https://api-m.paypal.com", allow_insecure_mock=False,
                    public_url="http://localhost:8000")
    r = c.post("/api/agents", json={"name": "v", "policy": pr.SHOP_POLICY, "daily_cap_usd": 250.0,
                                    "per_call_cap_usd": 120.0}, headers=A).json()
    assert L.set_funding_model(r["agent_id"], "vendor", 10_000)[0]
    L.set_vault(r["agent_id"], "VAULT-FAKE", "VISA 1111")
    fulfils = []
    live(monkeypatch, lambda *x, **k: fulfils.append(1) or {})
    rr = c.post("/api/purchases", json=NAPKINS, headers={"X-Agent-Key": r["api_key"]})
    assert rr.status_code == 503 and "sandbox" in rr.text
    assert fulfils == [] and L.purchases(r["agent_id"]) == []


# ---------- 6. double approval ----------
def test_two_concurrent_approvals_one_payment(env, monkeypatch):
    c, pp, L = env
    a = agent(c, L)
    pid = buy(c, a, BLANKETS).json()["purchase_id"]
    fulfils = []
    live(monkeypatch, lambda *x, **k: fulfils.append(1) or time.sleep(0.2) or {"ok": 1}, item="throw-blanket-sample")
    codes, bar = [], threading.Barrier(2)

    def go():
        bar.wait()
        codes.append(c.post(f"/api/purchases/{pid}/approve", headers=A).status_code)
    ts = [threading.Thread(target=go) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(codes) == [200, 409]
    assert fulfils == [1] and len(pays(pp)) == 1


def test_two_app_instances_approve_once(tmp_path, ids, monkeypatch):
    c1, pp1, L1 = make(tmp_path)
    c2, pp2, _ = make(tmp_path)                                  # same DB file, separate locks (two processes)
    share_paypal(pp1, pp2)
    a = agent(c1, L1)
    pid = buy(c1, a, BLANKETS).json()["purchase_id"]
    live(monkeypatch, lambda *x, **k: time.sleep(0.2) or {"ok": 1}, item="throw-blanket-sample")
    codes, bar = [], threading.Barrier(2)

    def go(cl):
        bar.wait()
        codes.append(cl.post(f"/api/purchases/{pid}/approve", headers=A).status_code)
    ts = [threading.Thread(target=go, args=(cl,)) for cl in (c1, c2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(codes) == [200, 409] and len(pays(pp1)) + len(pays(pp2)) == 1


# ---------- 5. parallel buys can't exceed caps ----------
def _parallel(clients, a, n=10):
    codes, bar = [], threading.Barrier(n)

    def go(i):
        bar.wait()
        codes.append(buy(clients[i % len(clients)], a).status_code)
    ts = [threading.Thread(target=go, args=(i,)) for i in range(n)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    return codes


@pytest.mark.parametrize("cap", ["daily", "vendor", "envelope"])
def test_parallel_buys_never_exceed_caps(tmp_path, ids, monkeypatch, cap):
    c, pp, L = make(tmp_path, purchases_per_agent_hour=100, tool_calls_per_ip_hour=100)
    kw = {"daily": dict(daily=100.0), "vendor": dict(vendor_budgets={"samplebox": 100.0}),
          "envelope": dict(envelope=100.0)}[cap]
    a = agent(c, L, **({"envelope": 1000.0} | kw))
    live(monkeypatch, lambda *x, **k: time.sleep(0.05) or {"ok": 1})
    codes = _parallel([c], a)
    assert codes.count(200) == 4 and set(codes) <= {200, 403, 409}
    assert counted(L, a["agent_id"]) == 9600 and len(pays(pp)) == 4


def test_parallel_buys_two_app_instances_one_db(tmp_path, ids, monkeypatch):
    c1, pp1, L = make(tmp_path, purchases_per_agent_hour=100, tool_calls_per_ip_hour=100)
    c2, pp2, _ = make(tmp_path, purchases_per_agent_hour=100, tool_calls_per_ip_hour=100)
    share_paypal(pp1, pp2)
    a = agent(c1, L, envelope=100.0)
    live(monkeypatch, lambda *x, **k: time.sleep(0.05) or {"ok": 1})
    codes = _parallel([c1, c2], a)
    assert codes.count(200) <= 4 and set(codes) <= {200, 403, 409}
    assert counted(L, a["agent_id"]) <= 10000 and len(pays(pp1)) + len(pays(pp2)) == codes.count(200)


# ---------- 15. flag off ----------
def test_flag_off_vendor_agent_stays_blocked(tmp_path, ids):
    c, pp, L = make(tmp_path)
    a = agent(c, L)
    c2, pp2, L2 = make(tmp_path, vendor_payouts=False)
    r = buy(c2, a)
    assert r.status_code == 503 and r.json()["detail"]["error"] == "vendor_payouts_disabled"
    assert pp2.mock_requests == [] and pp2.mock_capture_count == 0 and money_entries(L2, a["agent_id"]) == []
    assert c2.put(f"/api/agents/{a['agent_id']}/funding", json={"model": "vendor", "envelope_usd": 5.0},
                  headers=A).status_code == 404
    pid = buy(c, a, BLANKETS).json()["purchase_id"]
    assert c2.post(f"/api/purchases/{pid}/approve", headers=A).status_code == 503 and pp2.mock_requests == []


def test_vendor_fields_only_with_flag(tmp_path, ids):
    con, _, Lon = make(tmp_path)
    a = agent(con, Lon)
    assert "vendors_payable" in con.get("/healthz").json()
    assert con.get("/api/me", headers={"X-Agent-Key": a["api_key"]}).json()["funding_model"] == "vendor"
    c, pp, L = make(tmp_path, vendor_payouts=False)
    assert "vendors_payable" not in c.get("/healthz").json()
    for key in (a["api_key"], c.post("/api/agents", json={"name": "t", "policy": pr.SHOP_POLICY, "daily_cap_usd": 10.0,
                                                          "per_call_cap_usd": 5.0}, headers=A).json()["api_key"]):
        me = c.get("/api/me", headers={"X-Agent-Key": key}).json()
        assert "funding_model" not in me and "envelope" not in me


# ---------- healthz ----------
def test_healthz_vendors_payable_count_only(env, monkeypatch):
    c, pp, L = env
    r = c.get("/healthz")
    assert r.json()["vendors_payable"] == 3
    no_ids_in(r.text)
    monkeypatch.delenv("VENDOR_SHOPSUITE_MERCHANT_ID")
    assert c.get("/healthz").json()["vendors_payable"] == 2


# ---------- funding endpoint ----------
def test_funding_endpoint_owner_only_and_whole_cents(env):
    c, pp, L = env
    r = c.post("/api/agents", json={"name": "t", "policy": pr.SHOP_POLICY, "daily_cap_usd": 10.0, "per_call_cap_usd": 5.0},
               headers=A).json()
    url = f"/api/agents/{r['agent_id']}/funding"
    assert c.put(url, json={"model": "vendor", "envelope_usd": 5.0}, headers={"X-Agent-Key": r["api_key"]}).status_code == 401
    assert c.put(url, json={"model": "vendor", "envelope_usd": 5.001}).status_code == 401
    assert c.put(url, json={"model": "direct", "envelope_usd": 5.0}, headers=A).status_code == 422
    assert c.put(url, json={"model": "vendor", "envelope_usd": -1}, headers=A).status_code == 422
    assert c.put(url, json={"model": "vendor", "envelope_usd": 5.001}, headers=A).status_code == 422   # whole cents
    assert c.put(url, json={"model": "vendor"}, headers=A).status_code == 422
    ok = c.put(url, json={"model": "vendor", "envelope_usd": 12.34}, headers=A)
    assert ok.status_code == 200 and L.agent(r["agent_id"])["envelope_cents_30d"] == 1234
    assert ok.json()["funding_model"] == "vendor" and ok.json()["envelope"]["budget_30d_usd"] == 12.34


def test_funding_endpoint_refuses_cutover_with_tab_money(env):
    c, pp, L = env
    r = c.post("/api/agents", json={"name": "t", "policy": pr.SHOP_POLICY, "daily_cap_usd": 10.0, "per_call_cap_usd": 5.0},
               headers=A).json()
    c.post(f"/api/agents/{r['agent_id']}/topup-sandbox-card", json={"amount_usd": 5.0}, headers=A)
    assert L.balance(r["agent_id"]) > 0
    rr = c.put(f"/api/agents/{r['agent_id']}/funding", json={"model": "vendor", "envelope_usd": 5.0}, headers=A)
    assert rr.status_code == 409 and L.agent(r["agent_id"])["funding_model"] == "tab"


def test_me_shows_envelope_never_ids(env):
    c, pp, L = env
    a = agent(c, L)
    buy(c, a)
    me = c.get("/api/me", headers={"X-Agent-Key": a["api_key"]})
    j = me.json()
    assert j["funding_model"] == "vendor"
    assert j["envelope"] == {"budget_30d_usd": 100.0, "used_30d_usd": 24.0, "remaining_usd": 76.0}
    no_ids_in(me.text)
    lst = c.get("/api/me/purchases", headers={"X-Agent-Key": a["api_key"]})
    no_ids_in(lst.text)
    assert all("payee_merchant_id" not in p for p in lst.json())
    no_ids_in(c.get(f"/api/purchases?agent_id={a['agent_id']}", headers=A).text)


# ---------- money paths that must never treat vendor money as ours ----------
def test_refund_of_vendor_paid_purchase_never_credits_tab_or_calls_paypal(env):
    c, pp, L = env
    a = agent(c, L)
    pid = buy(c, a).json()["purchase_id"]
    r = c.post(f"/api/purchases/{pid}/refund", headers=A)
    assert r.status_code == 409
    assert money_entries(L, a["agent_id"]) == [] and pp.mock_refund_log == []
    assert L.purchase(pid)["status"] == "completed"


def test_refunded_webhook_for_vendor_capture_never_credits_tab(env):
    c, pp, L = env
    a = agent(c, L)
    pid = buy(c, a).json()["purchase_id"]
    cap = L.purchase(pid)["paypal_capture"]
    res = {"id": "REF-1", "amount": {"value": "24.00", "currency_code": "USD"},
           "links": [{"rel": "up", "href": f"https://api-m.sandbox.paypal.com/v2/payments/captures/{cap}"}]}
    r = c.post("/paypal/webhook", json={"id": "WH-vp1", "event_type": "PAYMENT.CAPTURE.REFUNDED", "resource": res})
    assert r.status_code == 200
    assert money_entries(L, a["agent_id"]) == [] and L.balance(a["agent_id"]) == 0
    assert L.purchase(pid)["status"] == "completed"


def test_tab_agent_flow_unchanged_with_flag_on(tmp_path, ids):
    """Flag on, but a tab agent: still the tab (a 402 asking for a top-up), never a vendor payment."""
    c, pp, L = make(tmp_path)
    r = c.post("/api/agents", json={"name": "t", "policy": pr.SHOP_POLICY, "daily_cap_usd": 250.0,
                                    "per_call_cap_usd": 120.0}, headers=A).json()
    rr = c.post("/api/purchases", json=NAPKINS, headers={"X-Agent-Key": r["api_key"]})
    assert rr.status_code == 402 and rr.json()["error"] == "payment_required" and pays(pp) == []
