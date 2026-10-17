"""PayPal depth: authorize-then-capture, forced declines (PayPal-Mock-Response), reconciliation freeze,
and saved-card (Vault) top-ups. Offline (mock PayPal) unless LIVE_PAYPAL=1."""
import os
import time
from decimal import Decimal

import pytest

from app import procurement as pr
from app.config import Settings
from app.paypal import PayPalClient, PayPalError
from tests.test_agenttab import A, _demo_client, _wh, buy, fund, shop_agent
from tests.test_hardening import _client

SEAT = dict(vendor="shopsuite", item="inventory-seat", quantity=2, purpose="Inventory software to track stock across our two warehouses")
MUG = dict(vendor="samplebox", item="ceramic-mug-sample")


def _approval(c, ag):
    j = buy(c, ag["api_key"], **SEAT).json()
    assert j["status"] == "approval_required"
    return j["purchase_id"], j["approve_url"].rsplit("/", 1)[1]


def _money(c, aid):
    return [e for e in c.app.state.ledger.entries(aid) if e["kind"] in ("fund", "charge", "refund", "credit")]


# ---------- authorize, deliver, then capture ----------
def test_approval_link_authorizes_then_captures_after_delivery(tmp_path):
    c = _client(tmp_path)
    ag = shop_agent(c)
    pid, oid = _approval(c, ag)
    assert c.app.state.ledger.order(oid)["intent"] == "AUTHORIZE"
    c.get(f"/mock-paypal/approve/{oid}")
    assert "approved=" in c.get(f"/paypal/return?token={oid}", follow_redirects=False).headers["location"]
    pu, pp = c.app.state.ledger.purchase(pid), c.app.state.paypal
    assert pu["status"] == "completed" and pu["paypal_authorization"] and pu["paypal_capture"]
    assert pp.mock_capture_count == 0 and pp.mock_authorize_count == 1      # held, never order-captured
    assert pp._mock_auths[pu["paypal_authorization"]]["status"] == "CAPTURED"
    kinds = sorted(e["kind"] for e in _money(c, ag["agent_id"]))
    assert kinds == ["charge", "fund"] and c.app.state.ledger.balance(ag["agent_id"]) == 0
    # refund goes back on the authorization's capture
    r = c.post(f"/api/purchases/{pid}/refund", headers=A).json()
    assert r["to"] == "paypal" and pp.mock_refund_log == [(pu["paypal_capture"], Decimal("78.00"))]


def test_vendor_failure_voids_hold_and_charges_nothing(tmp_path, monkeypatch):
    c = _client(tmp_path)
    ag = shop_agent(c)
    pid, oid = _approval(c, ag)
    monkeypatch.setitem(pr.VENDORS["shopsuite"], "fulfillment", "live")       # make delivery go through TOOLS

    def boom(*a, **k):
        raise RuntimeError("vendor down")
    monkeypatch.setitem(__import__("app.main", fromlist=["TOOLS"]).TOOLS, "inventory-seat", boom)
    r = c.post(f"/api/purchases/{pid}/approve-sandbox-card", headers=A).json()
    pu, pp = c.app.state.ledger.purchase(pid), c.app.state.paypal
    assert r["status"] == "failed_voided" and "voided" in r["note"] and r["paypal_capture_id"] is None
    assert pp._mock_auths[pu["paypal_authorization"]]["status"] == "VOIDED"
    assert _money(c, ag["agent_id"]) == [] and pp.mock_refund_log == []       # no capture, so no refund needed


def test_capture_decline_after_delivery_freezes_agent(tmp_path):
    c = _client(tmp_path)
    ag = shop_agent(c)
    pid, _ = _approval(c, ag)
    r = c.post(f"/api/purchases/{pid}/approve-sandbox-card", headers=A | {"X-Simulate-Decline": "TRANSACTION_REFUSED"}).json()
    assert r["status"] == "capture_declined" and r["flag"] == "capture_declined"
    assert _money(c, ag["agent_id"]) == []
    me = c.get("/api/me", headers={"X-Agent-Key": ag["api_key"]}).json()
    assert me["frozen"] and "TRANSACTION_REFUSED" in me["frozen_reason"]
    fund(c, ag["agent_id"], 100)                                 # owner may still fund a frozen agent
    r = buy(c, ag["api_key"], **MUG)
    assert r.status_code == 423 and r.json()["detail"]["error"] == "agent_frozen"
    assert c.post("/api/me/topup-request", json={"amount_usd": 1}, headers={"X-Agent-Key": ag["api_key"]}).status_code == 423
    assert c.post(f"/api/agents/{ag['agent_id']}/unfreeze", headers=A).json()["frozen"] is False
    assert buy(c, ag["api_key"], **MUG).status_code == 200


def test_late_paid_link_on_expired_approval_is_voided(tmp_path):
    c = _client(tmp_path)
    ag = shop_agent(c)
    pid, oid = _approval(c, ag)
    c.app.state.ledger.update_purchase(pid, approval_expires=time.time() - 1)
    c.get(f"/mock-paypal/approve/{oid}")
    c.get(f"/paypal/return?token={oid}", follow_redirects=False)
    pu = c.app.state.ledger.purchase(pid)
    assert pu["status"] == "expired" and c.app.state.paypal._mock_auths[pu["paypal_authorization"]]["status"] == "VOIDED"
    assert _money(c, ag["agent_id"]) == []


def test_frozen_agent_hold_is_voided_not_captured(tmp_path):
    c = _client(tmp_path)
    ag = shop_agent(c)
    pid, oid = _approval(c, ag)
    c.post(f"/api/agents/{ag['agent_id']}/freeze", json={"reason": "owner investigating"}, headers=A)
    assert c.post(f"/api/purchases/{pid}/approve-sandbox-card", headers=A).status_code == 423
    c.get(f"/mock-paypal/approve/{oid}")                        # the payer pays the link anyway
    c.get(f"/paypal/return?token={oid}", follow_redirects=False)
    pu = c.app.state.ledger.purchase(pid)
    assert pu["status"] == "failed_voided" and "frozen" in pu["reason"] and _money(c, ag["agent_id"]) == []


def test_authorized_amount_must_match_approved_amount(tmp_path):
    c = _client(tmp_path)
    ag = shop_agent(c)
    pid, oid = _approval(c, ag)
    pp = c.app.state.paypal
    real = pp.authorize_order
    pp.authorize_order = lambda o: real(o) | {"amount": Decimal("0.01")}   # tampered/odd PayPal response
    c.get(f"/mock-paypal/approve/{oid}")
    c.get(f"/paypal/return?token={oid}", follow_redirects=False)
    pu = c.app.state.ledger.purchase(pid)
    assert pu["status"] == "failed_voided" and "approved amount" in pu["reason"] and _money(c, ag["agent_id"]) == []


def test_capture_webhook_for_authorize_order_does_not_complete_purchase(tmp_path):
    c = _client(tmp_path)
    ag = shop_agent(c)
    pid, oid = _approval(c, ag)
    ev = {"id": "CAP-FORGED", "amount": {"value": "78.00"}, "supplementary_data": {"related_ids": {"order_id": oid}}}
    _wh(c, "PAYMENT.CAPTURE.COMPLETED", ev)
    assert c.app.state.ledger.purchase(pid)["status"] == "pending_approval" and _money(c, ag["agent_id"]) == []


def test_order_capture_endpoint_refuses_authorize_order(tmp_path):
    c = _client(tmp_path)
    ag = shop_agent(c)
    _, oid = _approval(c, ag)
    c.get(f"/mock-paypal/approve/{oid}")
    with pytest.raises(PayPalError):
        c.app.state.paypal.capture_order(oid)                    # intent mismatch, like PayPal


# ---------- forced declines ----------
def test_simulated_decline_is_owner_and_allowlist_only(tmp_path):
    c = _demo_client(tmp_path)
    s = c.post("/api/demo/session").json()
    k = {"X-Agent-Key": s["agent_key"]}
    pid = c.post("/api/purchases", json={"purpose": "Inventory software for our stock", "vendor": "shopsuite",
                                         "item": "inventory-seat", "quantity": 2}, headers=k).json()["purchase_id"]
    r = c.post(f"/api/purchases/{pid}/approve-sandbox-card", headers=k | {"X-Simulate-Decline": "TRANSACTION_REFUSED"})
    assert r.status_code == 403 and c.app.state.ledger.purchase(pid)["status"] == "pending_approval"
    adm = {"X-Admin-Token": "x" * 32}
    r = c.post(f"/api/purchases/{pid}/approve-sandbox-card", headers=adm | {"X-Simulate-Decline": "ANYTHING_ELSE"})
    assert r.status_code == 422 and c.app.state.ledger.purchase(pid)["status"] == "pending_approval"


def test_card_topup_decline_credits_nothing(tmp_path):
    c = _client(tmp_path)
    ag = shop_agent(c)
    r = c.post(f"/api/agents/{ag['agent_id']}/topup-sandbox-card", json={"amount_usd": 2},
               headers=A | {"X-Simulate-Decline": "INSTRUMENT_DECLINED"})
    assert r.status_code == 402 and "INSTRUMENT_DECLINED" in r.text
    assert c.app.state.ledger.balance(ag["agent_id"]) == 0 and _money(c, ag["agent_id"]) == []


def test_checkout_capture_decline_releases_order_for_retry(tmp_path):
    c = _client(tmp_path)
    ag = shop_agent(c)
    o = c.post(f"/api/agents/{ag['agent_id']}/topup", json={"amount_usd": 2}, headers=A).json()
    c.get(f"/mock-paypal/approve/{o['order_id']}")
    c.app.state.paypal.mock_fail["capture_order"] = "INSTRUMENT_DECLINED"
    r = c.get(f"/paypal/return?token={o['order_id']}", follow_redirects=False)
    assert "payment_error=402" in r.headers["location"] and c.app.state.ledger.balance(ag["agent_id"]) == 0
    assert c.app.state.ledger.order(o["order_id"])["status"] == "CREATED"
    assert "funded=" in c.get(f"/paypal/return?token={o['order_id']}", follow_redirects=False).headers["location"]
    assert c.app.state.ledger.balance(ag["agent_id"]) == Decimal("2.00")


def test_refund_decline_keeps_purchase_completed_then_retry_works(tmp_path):
    c = _client(tmp_path)
    ag = shop_agent(c)
    pid, _ = _approval(c, ag)
    c.post(f"/api/purchases/{pid}/approve-sandbox-card", headers=A)
    r = c.post(f"/api/purchases/{pid}/refund", headers=A | {"X-Simulate-Decline": "REFUND_FAILED_INSUFFICIENT_FUNDS"})
    assert r.status_code == 502 and "REFUND_FAILED_INSUFFICIENT_FUNDS" in r.text
    assert c.app.state.ledger.purchase(pid)["status"] == "completed"
    assert [e["kind"] for e in _money(c, ag["agent_id"])].count("refund") == 0
    assert c.post(f"/api/purchases/{pid}/refund", headers=A).json()["refunded_usd"] == 78.0


def test_mock_response_header_only_in_sandbox_and_fresh_request_id(monkeypatch):
    live = PayPalClient(Settings(paypal_mode="sandbox", paypal_base="https://api-m.paypal.com",
                                 paypal_client_id="x", paypal_client_secret="y"))
    with pytest.raises(PayPalError):
        live._mock_header("INSTRUMENT_DECLINED")
    sb = PayPalClient(Settings(paypal_mode="sandbox", paypal_client_id="x", paypal_client_secret="y"))
    with pytest.raises(PayPalError):
        sb._mock_header('X","mock_application_codes":"Y')
    sb._access_token = lambda: "tok"
    seen = []

    class R:
        status_code, text = 422, '{"name":"UNPROCESSABLE_ENTITY","details":[{"issue":"TRANSACTION_REFUSED"}]}'
    monkeypatch.setattr("app.paypal.httpx.request", lambda m, u, json=None, headers=None, timeout=0: seen.append(headers) or R())
    with pytest.raises(PayPalError) as e:
        sb.capture_authorization("AUTH1", Decimal("0.40"), simulate="TRANSACTION_REFUSED")
    assert e.value.issue == "TRANSACTION_REFUSED"
    assert seen[0]["PayPal-Mock-Response"] == '{"mock_application_codes": "TRANSACTION_REFUSED"}'
    assert seen[0]["PayPal-Request-Id"].startswith("sim-")          # a drill never replays/poisons the real op id


# ---------- reconciliation freeze ----------
def test_reconcile_clean_then_outside_refund_freezes(tmp_path):
    c = _client(tmp_path)
    ag = shop_agent(c)
    aid = ag["agent_id"]
    c.post(f"/api/agents/{aid}/topup-sandbox-card", json={"amount_usd": 2}, headers=A)
    pid, _ = _approval(c, ag)
    c.post(f"/api/purchases/{pid}/approve-sandbox-card", headers=A)
    c.post(f"/api/purchases/{pid}/refund", headers=A)
    r = c.post(f"/api/agents/{aid}/reconcile", headers=A).json()
    assert r["ok"] and r["checked"] == 2 and not r["frozen"]
    cap = c.app.state.ledger.captures(aid)[0]["paypal_ref"]
    c.app.state.paypal._mock_captures[cap]["status"] = "REFUNDED"     # refunded in PayPal, no webhook arrived
    r = c.post(f"/api/agents/{aid}/reconcile", headers=A).json()
    assert not r["ok"] and r["frozen_now"] and cap in r["mismatches"][0]
    assert buy(c, ag["api_key"], **MUG).status_code == 423


@pytest.mark.parametrize("status", ["REVERSED", "DECLINED", "PARTIALLY_REFUNDED"])
def test_reconcile_flags_each_paypal_side_change(tmp_path, status):
    c = _client(tmp_path)
    ag = shop_agent(c)
    c.post(f"/api/agents/{ag['agent_id']}/topup-sandbox-card", json={"amount_usd": 1}, headers=A)
    cap = c.app.state.ledger.captures(ag["agent_id"])[0]["paypal_ref"]
    c.app.state.paypal._mock_captures[cap]["status"] = status
    r = c.post(f"/api/agents/{ag['agent_id']}/reconcile", headers=A).json()
    assert r["frozen"] and status.split("_")[-1].lower() in r["mismatches"][0].lower()


def test_reconcile_unknown_capture_and_amount_mismatch(tmp_path):
    c = _client(tmp_path)
    ag = shop_agent(c)
    led = c.app.state.ledger
    led.add_entry(ag["agent_id"], "fund", Decimal("3"), paypal_ref="CAP-NOT-AT-PAYPAL", reason="forged credit")
    r = c.post(f"/api/agents/{ag['agent_id']}/reconcile", headers=A).json()
    assert r["frozen"] and "no such capture" in r["mismatches"][0]
    ag2 = shop_agent(c)
    c.post(f"/api/agents/{ag2['agent_id']}/topup-sandbox-card", json={"amount_usd": 1}, headers=A)
    cap = led.captures(ag2["agent_id"])[0]["paypal_ref"]
    c.app.state.paypal._mock_captures[cap]["amount"] = Decimal("0.10")
    assert "amount" in c.post(f"/api/agents/{ag2['agent_id']}/reconcile", headers=A).json()["mismatches"][0]


def test_reconcile_lookup_error_is_unverified_not_frozen(tmp_path):
    c = _client(tmp_path)
    ag = shop_agent(c)
    c.post(f"/api/agents/{ag['agent_id']}/topup-sandbox-card", json={"amount_usd": 1}, headers=A)

    def flaky(cid):
        raise PayPalError("GET: 503", "", 503)
    c.app.state.paypal.get_capture = flaky
    r = c.post(f"/api/agents/{ag['agent_id']}/reconcile", headers=A).json()
    assert r["ok"] and r["unverified"] == 1 and not r["frozen"]


def test_reversal_webhook_freezes_and_negative_balance_reported(tmp_path):
    c = _client(tmp_path)
    ag = shop_agent(c)
    aid = ag["agent_id"]
    fund(c, aid, 100)
    assert buy(c, ag["api_key"], **MUG).status_code == 200
    cap = c.app.state.ledger.captures(aid)[0]["paypal_ref"]
    r = _wh(c, "PAYMENT.CAPTURE.REVERSED", {"id": cap, "amount": {"value": "100.00"}}).json()
    assert "frozen" in r["effect"]
    a = c.get(f"/api/agents", headers=A).json()[0]
    assert a["frozen"] and a["balance_usd"] == -18.0
    rec = c.post(f"/api/agents/{aid}/reconcile", headers=A).json()
    assert any("negative" in m for m in rec["mismatches"])


def test_outside_topup_refund_into_negative_freezes(tmp_path):
    c = _client(tmp_path)
    ag = shop_agent(c)
    aid = ag["agent_id"]
    fund(c, aid, 100)
    buy(c, ag["api_key"], **MUG)
    cap = c.app.state.ledger.captures(aid)[0]["paypal_ref"]
    _wh(c, "PAYMENT.CAPTURE.REFUNDED", {"id": "REF-OUT", "amount": {"value": "100.00"},
        "links": [{"rel": "up", "href": f"https://api.sandbox.paypal.com/v2/payments/captures/{cap}"}]})
    assert c.get("/api/me", headers={"X-Agent-Key": ag["api_key"]}).json()["frozen"]


def test_freeze_endpoints_are_admin_only(tmp_path):
    c = _client(tmp_path)
    ag = shop_agent(c)
    k = {"X-Agent-Key": ag["api_key"]}
    for path in ("freeze", "unfreeze", "reconcile", "saved-card", "topup-saved-card"):
        assert c.post(f"/api/agents/{ag['agent_id']}/{path}", json={"amount_usd": 1}, headers=k).status_code == 401
    assert c.put(f"/api/agents/{ag['agent_id']}/auto-topup", json={}, headers=k).status_code == 401
    c.post(f"/api/agents/{ag['agent_id']}/freeze", json={"reason": "first"}, headers=A)
    c.post(f"/api/agents/{ag['agent_id']}/freeze", json={"reason": "second"}, headers=A)
    assert c.get("/api/agents", headers=A).json()[0]["frozen_reason"] == "first"     # first reason kept


# ---------- saved card (Vault) ----------
def _saved(c, ag):
    r = c.post(f"/api/agents/{ag['agent_id']}/saved-card", headers=A)
    assert r.status_code == 200 and r.json()["saved_card"] == "VISA 1111"
    return r.json()


def test_saved_card_topup_and_token_never_exposed(tmp_path):
    c = _client(tmp_path)
    ag = shop_agent(c)
    aid = ag["agent_id"]
    _saved(c, ag)
    assert c.post(f"/api/agents/{aid}/saved-card", headers=A).status_code == 409
    r = c.post(f"/api/agents/{aid}/topup-saved-card", json={"amount_usd": 1.5}, headers=A).json()
    assert r["balance_usd"] == 1.5
    vid = c.app.state.ledger.agent(aid)["vault_id"]
    for url, h in (("/api/agents", A), ("/api/me", {"X-Agent-Key": ag["api_key"]}), (f"/api/ledger?agent_id={aid}", A)):
        assert vid not in c.get(url, headers=h).text
    r = c.post(f"/api/agents/{aid}/topup-saved-card", json={"amount_usd": 1}, headers=A | {"X-Simulate-Decline": "INSTRUMENT_DECLINED"})
    assert r.status_code == 402 and c.app.state.ledger.balance(aid) == Decimal("1.50")
    assert c.post(f"/api/agents/{aid}/reconcile", headers=A).json()["ok"]


def test_auto_topup_only_after_guard_allows_and_within_cap(tmp_path):
    c = _client(tmp_path)
    ag = shop_agent(c, daily_cap_usd=500.0, vendor_budgets={"samplebox": 500.0, "shopsuite": 500.0})
    aid, k = ag["agent_id"], ag["api_key"]
    assert c.put(f"/api/agents/{aid}/auto-topup", json={"amount_usd": 45, "monthly_cap_usd": 90}, headers=A).status_code == 409
    _saved(c, ag)
    assert c.put(f"/api/agents/{aid}/auto-topup", json={"amount_usd": 180, "monthly_cap_usd": 90}, headers=A).status_code == 422
    v = c.put(f"/api/agents/{aid}/auto-topup", json={"amount_usd": 45, "monthly_cap_usd": 90}, headers=A).json()
    assert v["auto_topup"]["monthly_cap_usd"] == 90.0
    # guard-blocked purchase: no money pulled
    r = buy(c, k, vendor="adboost-social", item="followers-1000", purpose="Boost my personal account")
    assert r.status_code == 403 and c.app.state.ledger.vault_topups_since(aid, 0) == 0
    seq = [buy(c, k, **MUG).status_code for _ in range(6)]
    assert seq == [200, 200, 200, 200, 200, 402]              # 2 auto top-ups of $45 = cap $90 (5 x $18 mugs)
    assert c.app.state.ledger.vault_topups_since(aid, 0) == Decimal("90.00")
    r = buy(c, k, **MUG).json()
    assert r["error"] == "payment_required" and "cap" in r["auto_topup"]


def test_auto_topup_card_decline_is_402_and_credits_nothing(tmp_path):
    c = _client(tmp_path)
    ag = shop_agent(c)
    aid = ag["agent_id"]
    _saved(c, ag)
    c.put(f"/api/agents/{aid}/auto-topup", json={"amount_usd": 45, "monthly_cap_usd": 90}, headers=A)
    c.app.state.paypal.mock_fail["authorize_vault"] = "INSTRUMENT_DECLINED"
    r = buy(c, ag["api_key"], **MUG)
    assert r.status_code == 402 and "INSTRUMENT_DECLINED" in r.json()["auto_topup"]
    assert c.app.state.ledger.balance(aid) == 0 and c.app.state.ledger.purchases(aid) == []


def test_auto_topup_never_runs_for_frozen_agent_and_delete_turns_it_off(tmp_path):
    c = _client(tmp_path)
    ag = shop_agent(c)
    aid = ag["agent_id"]
    _saved(c, ag)
    c.put(f"/api/agents/{aid}/auto-topup", json={"amount_usd": 45, "monthly_cap_usd": 135}, headers=A)
    c.post(f"/api/agents/{aid}/freeze", json={"reason": "x"}, headers=A)
    assert buy(c, ag["api_key"], **MUG).status_code == 423 and c.app.state.ledger.vault_topups_since(aid, 0) == 0
    v = c.delete(f"/api/agents/{aid}/saved-card", headers=A).json()
    assert v["saved_card"] is None and v["auto_topup"] is None
    assert c.app.state.ledger.agent(aid)["auto_topup_micro"] is None


def test_manual_saved_card_topups_count_toward_cap(tmp_path):
    c = _client(tmp_path)
    ag = shop_agent(c)
    aid = ag["agent_id"]
    _saved(c, ag)
    c.put(f"/api/agents/{aid}/auto-topup", json={"amount_usd": 45, "monthly_cap_usd": 90}, headers=A)
    assert c.post(f"/api/agents/{aid}/topup-saved-card", json={"amount_usd": 90}, headers=A).status_code == 200
    assert c.post(f"/api/agents/{aid}/topup-saved-card", json={"amount_usd": 0.01}, headers=A).status_code == 409


def test_demo_agent_cannot_use_vault_or_freeze_endpoints(tmp_path):
    c = _demo_client(tmp_path)
    s = c.post("/api/demo/session").json()
    k = {"X-Agent-Key": s["agent_key"]}
    assert c.post(f"/api/agents/{s['agent_id']}/saved-card", headers=k).status_code == 401
    assert c.post(f"/api/agents/{s['agent_id']}/unfreeze", headers=k).status_code == 401


# ---------- live sandbox ----------
@pytest.mark.skipif(os.getenv("LIVE_PAYPAL") != "1", reason="set LIVE_PAYPAL=1 to hit the real PayPal sandbox")
def test_live_sandbox_authorize_capture_void_declines_and_vault():
    pp = PayPalClient(Settings(paypal_mode="sandbox"))
    op = os.urandom(5).hex()
    a = pp.card_authorize(Decimal("0.40"), "ag_live", op_id=f"t1-{op}")
    cap = pp.capture_authorization(a["authorization_id"], Decimal("0.40"))
    assert cap["status"] == "COMPLETED" and pp.get_capture(cap["capture_id"])["status"] == "COMPLETED"
    with pytest.raises(PayPalError) as e:
        pp.refund_capture(cap["capture_id"], Decimal("0.40"), op_id="t", simulate="REFUND_FAILED_INSUFFICIENT_FUNDS")
    assert e.value.issue == "REFUND_FAILED_INSUFFICIENT_FUNDS"
    pp.refund_capture(cap["capture_id"], Decimal("0.40"), op_id=f"t1-{op}")
    assert pp.get_capture(cap["capture_id"])["status"] == "REFUNDED"
    b = pp.card_authorize(Decimal("0.40"), "ag_live", op_id=f"t2-{op}")
    with pytest.raises(PayPalError) as e:
        pp.capture_authorization(b["authorization_id"], Decimal("0.40"), simulate="TRANSACTION_REFUSED")
    assert e.value.issue == "TRANSACTION_REFUSED"
    assert pp.void_authorization(b["authorization_id"]) == "VOIDED"
    with pytest.raises(PayPalError) as e:
        pp.sandbox_card_topup(Decimal("1.00"), "ag_live", op_id=f"t3-{op}", simulate="INSTRUMENT_DECLINED")
    assert e.value.issue == "INSTRUMENT_DECLINED"
    v = pp.vault_sandbox_card(op_id=f"t4-{op}")
    assert v["last4"] == "1111"
    ch = pp.charge_vault(v["vault_id"], Decimal("1.00"), "ag_live", op_id=f"t4-{op}")
    assert pp.get_capture(ch["capture_id"])["status"] == "COMPLETED"
    pp.refund_capture(ch["capture_id"], Decimal("1.00"), op_id=f"t4-{op}")
    h = pp.authorize_vault(v["vault_id"], Decimal("0.50"), "ag_live", op_id=f"t6-{op}")     # auto top-up: hold first
    assert h["amount"] == Decimal("0.50")
    hc = pp.capture_authorization(h["authorization_id"], Decimal("0.50"))
    assert pp.get_capture(hc["capture_id"])["status"] == "COMPLETED"
    pp.refund_capture(hc["capture_id"], Decimal("0.50"), op_id=f"t6-{op}")
    h2 = pp.authorize_vault(v["vault_id"], Decimal("0.50"), "ag_live", op_id=f"t7-{op}")
    assert pp.void_authorization(h2["authorization_id"]) == "VOIDED"
    pp.delete_vault_token(v["vault_id"])
    with pytest.raises(PayPalError):
        pp.charge_vault(v["vault_id"], Decimal("1.00"), "ag_live", op_id=f"t5-{op}")


# ---------- concurrency ----------
def test_return_url_and_webhook_race_authorizes_and_captures_once(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    c = _client(tmp_path)
    ag = shop_agent(c)
    pid, oid = _approval(c, ag)
    c.get(f"/mock-paypal/approve/{oid}")
    pp = c.app.state.paypal
    real = pp.authorize_order

    def slow(o):
        time.sleep(0.2)
        return real(o)
    pp.authorize_order = slow
    with ThreadPoolExecutor(6) as ex:
        list(ex.map(lambda i: c.get(f"/paypal/return?token={oid}", follow_redirects=False) if i % 2
                    else _wh(c, "CHECKOUT.ORDER.APPROVED", {"id": oid}), range(6)))
    kinds = sorted(e["kind"] for e in _money(c, ag["agent_id"]))
    assert pp.mock_authorize_count == 1 and kinds == ["charge", "fund"]
    assert c.app.state.ledger.purchase(pid)["status"] == "completed"


def test_parallel_purchases_never_auto_topup_past_cap(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    c = _client(tmp_path)
    ag = shop_agent(c, daily_cap_usd=500.0, vendor_budgets={"samplebox": 500.0})
    aid = ag["agent_id"]
    _saved(c, ag)
    c.put(f"/api/agents/{aid}/auto-topup", json={"amount_usd": 45, "monthly_cap_usd": 90}, headers=A)
    with ThreadPoolExecutor(8) as ex:
        codes = list(ex.map(lambda _: buy(c, ag["api_key"], **MUG).status_code, range(10)))
    led = c.app.state.ledger
    assert led.vault_topups_since(aid, 0) <= Decimal("90.00") and led.balance(aid) >= 0
    assert codes.count(200) == 5 and set(codes) <= {200, 402, 429}
