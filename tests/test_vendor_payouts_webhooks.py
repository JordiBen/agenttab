"""Vendor payouts, step 4: PayPal webhooks for vendor-paid captures (docs/vendor-payouts-impl-plan.md, "Webhooks").
A webhook body is never trusted on its own: it only tells AgentTab which vendor purchase to re-read from PayPal
(capture or order GET, the same checks as reconcile). Vendor events never credit the tab and never unfreeze.
Mock PayPal only (offline webhooks are unverified, which is exactly why the body is never trusted)."""
from __future__ import annotations

import json
import time
from decimal import Decimal

import pytest

from test_vendor_payouts_flow import A, MID, agent, buy, counted, env, ids, make, money_entries, no_ids_in, pays  # noqa: F401

H = 3600
_n = [0]


def hook(c, typ, resource, eid=None):
    _n[0] += 1
    return c.post("/paypal/webhook", json={"id": eid or f"WH-vp-{_n[0]}-{time.time_ns()}", "event_type": typ,
                                           "resource": resource})


def bought(c, L, **kw):
    a = agent(c, L, **kw)
    r = buy(c, a)
    assert r.status_code == 200, r.text
    return a, r.json()["purchase_id"]


def cap_res(L, pid, status="COMPLETED", value="24.00", cid=None):
    pu = L.purchase(pid)
    return {"id": cid or pu["paypal_capture"], "status": status, "custom_id": pid,
            "amount": {"currency_code": "USD", "value": value},
            "supplementary_data": {"related_ids": {"order_id": pu["paypal_order"] or ""}}}


def refund_res(L, pid, value="24.00"):
    cid = L.purchase(pid)["paypal_capture"]
    return {"id": "REFUND-" + cid[-6:], "status": "COMPLETED", "amount": {"currency_code": "USD", "value": value},
            "links": [{"rel": "up", "href": f"https://api-m.sandbox.paypal.com/v2/payments/captures/{cid}"}]}


def cap(pp, L, pid):
    return pp._mock_captures[L.purchase(pid)["paypal_capture"]]


def pending(c, pp, L):
    a = agent(c, L)
    pp.mock_vendor_tamper = lambda o: o["purchase_units"][0]["payments"]["captures"][0].update(status="PENDING")
    r = buy(c, a)
    assert r.status_code == 200 and r.json()["status"] == "payment_pending", r.text
    pp.mock_vendor_tamper = None
    return a, r.json()["purchase_id"]


def stored_order(pp, pid):
    return pp.mock_vendor_orders[f"at-{pid}-pay"]


def unknown(c, pp, L):
    a = agent(c, L)
    pp.mock_vendor_script = ["timeout_before", "timeout_before"]
    r = buy(c, a)
    assert r.status_code == 502, r.text
    return a, r.json()["detail"]["purchase_id"]


def no_tab_money(pp, L, a):
    assert money_entries(L, a["agent_id"]) == [] and pp.mock_refund_log == []


# ---------- REFUNDED ----------
def test_refunded_webhook_rereads_the_capture_and_marks_refunded(env):
    c, pp, L = env
    a, pid = bought(c, L)
    c.post(f"/api/purchases/{pid}/refund", headers=A)
    cap(pp, L, pid)["status"] = "REFUNDED"
    r = hook(c, "PAYMENT.CAPTURE.REFUNDED", refund_res(L, pid))
    assert r.status_code == 200, r.text
    pu = L.purchase(pid)
    assert pu["status"] == "refunded" and pu["refunded_ts"] and counted(L, a["agent_id"]) == 0
    no_tab_money(pp, L, a)
    no_ids_in(r.text)


def test_refunded_webhook_without_paypal_agreeing_changes_nothing(env):
    """The capture GET still says COMPLETED: the webhook body alone never refunds anything."""
    c, pp, L = env
    a, pid = bought(c, L)
    r = hook(c, "PAYMENT.CAPTURE.REFUNDED", refund_res(L, pid))
    assert r.status_code == 200
    assert r.json()["effect"].startswith("vendor purchase") and "nothing changed" in r.json()["effect"]
    assert L.purchase(pid)["status"] == "completed" and counted(L, a["agent_id"]) == 2400
    no_tab_money(pp, L, a)


def test_refunded_webhook_partial_reads_the_refunded_amount(env):
    c, pp, L = env
    a, pid = bought(c, L)
    cap(pp, L, pid)["status"] = "PARTIALLY_REFUNDED"
    stored_order(pp, pid)["purchase_units"][0]["payments"]["refunds"] = [
        {"id": "R1", "status": "COMPLETED", "amount": {"currency_code": "USD", "value": "4.00"}}]
    hook(c, "PAYMENT.CAPTURE.REFUNDED", refund_res(L, pid, "4.00"))
    pu = L.purchase(pid)
    assert pu["status"] == "partially_refunded" and pu["refunded_cents"] == 400 and counted(L, a["agent_id"]) == 2000
    no_tab_money(pp, L, a)


def test_refunded_webhook_found_by_custom_id(env):
    c, pp, L = env
    a, pid = bought(c, L)
    cap(pp, L, pid)["status"] = "REFUNDED"
    res = {"id": "REFUND-X1", "status": "COMPLETED", "custom_id": pid,
           "amount": {"currency_code": "USD", "value": "24.00"}, "links": []}
    hook(c, "PAYMENT.CAPTURE.REFUNDED", res)
    assert L.purchase(pid)["status"] == "refunded"


# ---------- COMPLETED ----------
def test_completed_webhook_settles_a_pending_payment_after_reading_the_order(env):
    c, pp, L = env
    a, pid = pending(c, pp, L)
    hook(c, "PAYMENT.CAPTURE.COMPLETED", cap_res(L, pid))
    assert L.purchase(pid)["status"] == "payment_pending"           # PayPal's order still says PENDING
    stored_order(pp, pid)["purchase_units"][0]["payments"]["captures"][0]["status"] = "COMPLETED"
    hook(c, "PAYMENT.CAPTURE.COMPLETED", cap_res(L, pid))
    pu = L.purchase(pid)
    assert pu["status"] == "completed" and pu["refund_deadline"]
    no_tab_money(pp, L, a)


def test_completed_webhook_verifies_an_unknown_payment(env):
    c, pp, L = env
    a, pid = unknown(c, pp, L)
    pp.mock_vendor_script = []
    # the order PayPal actually made (the answer was lost): create it in the mock as PayPal would have
    pp.mock_vendor_script = ["timeout_after"]
    try:
        pp.pay_vendor(L.purchase(pid)["pay_vault_id"], 2400, MID["samplebox"], pid, "x")
    except Exception:
        pass
    o = stored_order(pp, pid)
    cid = o["purchase_units"][0]["payments"]["captures"][0]["id"]
    n = len(pays(pp))
    hook(c, "PAYMENT.CAPTURE.COMPLETED", {"id": cid, "status": "COMPLETED", "custom_id": pid,
                                          "amount": {"currency_code": "USD", "value": "24.00"}})
    pu = L.purchase(pid)
    assert pu["status"] == "completed" and pu["paypal_capture"] == cid and len(pays(pp)) == n
    assert L.agent(a["agent_id"])["frozen_reason"]                   # never unfreezes
    no_tab_money(pp, L, a)


@pytest.mark.parametrize("change", ["payee", "amount", "custom_id", "missing"])
def test_completed_webhook_for_unknown_payment_with_a_bad_capture_changes_nothing(env, change):
    c, pp, L = env
    a, pid = unknown(c, pp, L)
    cid = "CAPFORGED001"
    if change != "missing":
        pp._mock_captures[cid] = {"amount": Decimal("24.00"), "refunded": Decimal(0), "status": "COMPLETED",
                                  "payee": "OTHERMERCH001" if change == "payee" else MID["samplebox"],
                                  "custom_id": "pu_aaaaaaaaaaaa" if change == "custom_id" else pid}
        if change == "amount":
            pp._mock_captures[cid]["amount"] = Decimal("1.00")
    r = hook(c, "PAYMENT.CAPTURE.COMPLETED", {"id": cid, "status": "COMPLETED", "custom_id": pid,
                                              "amount": {"currency_code": "USD", "value": "24.00"}})
    assert r.status_code == 200 and "not verified" in r.json()["effect"], r.text
    assert L.purchase(pid)["status"] == "payment_unknown" and not L.purchase(pid)["paypal_capture"]


def test_completed_webhook_on_a_failed_payment_flags_and_keeps_counting(env):
    """PayPal says a capture exists for a purchase we recorded as refused: money may have moved. Flag + freeze."""
    c, pp, L = env
    a = agent(c, L)
    pp.mock_vendor_script = [("error", 422, "INSTRUMENT_DECLINED")]
    r = buy(c, a)
    pid = r.json()["detail"]["purchase_id"]
    cid = "CAPLATE00001"
    pp._mock_captures[cid] = {"amount": Decimal("24.00"), "refunded": Decimal(0), "status": "COMPLETED",
                              "payee": MID["samplebox"], "custom_id": pid}
    hook(c, "PAYMENT.CAPTURE.COMPLETED", {"id": cid, "status": "COMPLETED", "custom_id": pid,
                                          "amount": {"currency_code": "USD", "value": "24.00"}})
    pu = L.purchase(pid)
    assert pu["status"] == "flagged" and pu["paypal_capture"] == cid and counted(L, a["agent_id"]) == 2400
    assert L.agent(a["agent_id"])["frozen_reason"]
    no_tab_money(pp, L, a)


def test_completed_webhook_on_a_completed_purchase_is_only_noted(env):
    c, pp, L = env
    a, pid = bought(c, L)
    before = L.purchase(pid)["status"]
    r = hook(c, "PAYMENT.CAPTURE.COMPLETED", cap_res(L, pid))
    assert r.status_code == 200 and L.purchase(pid)["status"] == before
    assert r.json()["effect"].startswith("vendor purchase") and L.purchase(pid)["paypal_confirmed"]
    assert not L.agent(a["agent_id"])["frozen_reason"]
    no_tab_money(pp, L, a)


# ---------- DENIED / REVERSED / DECLINED ----------
def test_denied_webhook_on_a_pending_payment_flags_and_freezes(env):
    c, pp, L = env
    a, pid = pending(c, pp, L)
    stored_order(pp, pid)["purchase_units"][0]["payments"]["captures"][0]["status"] = "DECLINED"
    hook(c, "PAYMENT.CAPTURE.DENIED", cap_res(L, pid, status="DECLINED"))
    pu = L.purchase(pid)
    assert pu["status"] == "flagged" and counted(L, a["agent_id"]) == 2400
    assert L.agent(a["agent_id"])["frozen_reason"]
    no_tab_money(pp, L, a)


def test_reversed_webhook_on_a_completed_purchase_flags_freezes_and_keeps_counting(env):
    c, pp, L = env
    a, pid = bought(c, L)
    cap(pp, L, pid)["status"] = "REVERSED"
    r = hook(c, "PAYMENT.CAPTURE.REVERSED", cap_res(L, pid, status="REVERSED"))
    assert r.json()["effect"].startswith("vendor purchase"), r.text
    pu = L.purchase(pid)
    assert pu["status"] == "completed" and pu["flag"] == "reversed" and counted(L, a["agent_id"]) == 2400
    assert L.agent(a["agent_id"])["frozen_reason"]
    no_tab_money(pp, L, a)


def test_forged_reversed_webhook_changes_nothing(env):
    c, pp, L = env
    a, pid = bought(c, L)
    hook(c, "PAYMENT.CAPTURE.REVERSED", cap_res(L, pid, status="REVERSED"))
    assert not L.purchase(pid)["flag"] and not L.agent(a["agent_id"])["frozen_reason"]


# ---------- never a tab credit, idempotent, flag off, unavailable ----------
@pytest.mark.parametrize("typ", ["PAYMENT.CAPTURE.COMPLETED", "PAYMENT.CAPTURE.REFUNDED", "PAYMENT.CAPTURE.DENIED",
                                 "PAYMENT.CAPTURE.REVERSED", "PAYMENT.CAPTURE.PENDING", "PAYMENT.CAPTURE.DECLINED"])
def test_vendor_events_never_credit_the_tab(env, typ):
    c, pp, L = env
    a, pid = bought(c, L)
    cap(pp, L, pid)["status"] = {"PAYMENT.CAPTURE.REFUNDED": "REFUNDED"}.get(typ, "COMPLETED")
    res = refund_res(L, pid) if typ.endswith("REFUNDED") else cap_res(L, pid, status=typ.rsplit(".", 1)[1])
    r = hook(c, typ, res)
    assert r.status_code == 200, r.text
    no_tab_money(pp, L, a)
    assert r.json()["effect"].startswith("vendor purchase"), r.text
    no_ids_in(r.text)


def test_duplicate_event_is_applied_once(env):
    c, pp, L = env
    a, pid = bought(c, L)
    cap(pp, L, pid)["status"] = "REFUNDED"
    assert hook(c, "PAYMENT.CAPTURE.REFUNDED", refund_res(L, pid), eid="WH-dup-1").json()["effect"].startswith("vendor purchase")
    assert L.purchase(pid)["status"] == "refunded"
    assert hook(c, "PAYMENT.CAPTURE.REFUNDED", refund_res(L, pid), eid="WH-dup-1").json().get("duplicate") is True


def test_paypal_unavailable_during_the_reread_changes_nothing(env):
    c, pp, L = env
    a, pid = bought(c, L)
    cap(pp, L, pid)["status"] = "REFUNDED"
    pp.mock_fail["get_capture"] = "SERVICE_UNAVAILABLE"
    r = hook(c, "PAYMENT.CAPTURE.REFUNDED", refund_res(L, pid))
    assert r.status_code == 200 and "reconcile" in r.json()["effect"]
    assert L.purchase(pid)["status"] == "completed"


def test_flag_off_vendor_events_change_nothing(tmp_path, ids):
    c, pp, L = make(tmp_path)
    a, pid = bought(c, L)
    cap(pp, L, pid)["status"] = "REFUNDED"
    res = refund_res(L, pid)
    c2, pp2, L2 = make(tmp_path, vendor_payouts=False)      # same DB, flag off
    pp2._mock_captures.update(pp._mock_captures)
    r = hook(c2, "PAYMENT.CAPTURE.REFUNDED", res)
    assert r.status_code == 200 and "off" in r.json()["effect"]
    assert L2.purchase(pid)["status"] == "completed" and money_entries(L2, a["agent_id"]) == []
