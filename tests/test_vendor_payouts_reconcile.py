"""Vendor payouts, capture-only (docs/vendor-payouts-impl-plan.md), step 3: refund requests and reconcile.
Mock PayPal only; no network, no AI provider, no live site."""
from __future__ import annotations

import json
import time

import pytest

from test_vendor_payouts_flow import (A, BLANKETS, MID, NAPKINS, agent, buy, counted, env, ids, live, make,  # noqa: F401
                                      money_entries, no_ids_in, pays)

H = 3600


def refund(c, pid, headers=A, **kw):
    return c.post(f"/api/purchases/{pid}/refund", headers=headers, **kw)


def reconcile(c, a):
    return c.post(f"/api/agents/{a['agent_id']}/reconcile", headers=A)


def bought(c, L, **kw):
    a = agent(c, L, **kw)
    r = buy(c, a)
    assert r.status_code == 200, r.text
    return a, r.json()["purchase_id"]


def unknown(c, pp, L, script=("timeout_before", "timeout_before")):
    a = agent(c, L)
    pp.mock_vendor_script = list(script)
    r = buy(c, a)
    assert r.status_code == 502 and r.json()["detail"]["error"] == "payment_unknown", r.text
    return a, r.json()["detail"]["purchase_id"]


def vrows(r):
    return {v["purchase_id"]: v for v in r.json()["vendor_purchases"]}


# ================= refund requests =================
def test_refund_request_moves_to_refund_requested_without_paypal(env):
    c, pp, L = env
    a, pid = bought(c, L)
    before = len(pp.mock_requests)
    r = refund(c, pid, json={"reason": "samples arrived damaged"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["status"] == "refund_requested" and j["with"] == "SampleBox Wholesale"
    assert "with SampleBox Wholesale" in j["note"] and "never holds" in j["note"]
    assert len(pp.mock_requests) == before and pp.mock_refund_log == []          # no PayPal call at all
    assert money_entries(L, a["agent_id"]) == []                                  # no tab credit
    pu = L.purchase(pid)
    assert pu["status"] == "refund_requested" and pu["refund_requested_by"] == "owner"
    assert pu["refund_note"] == "samples arrived damaged" and pu["refund_requested_ts"]
    assert counted(L, a["agent_id"]) == 2400                                      # counts until confirmed
    no_ids_in(r.text)


def test_refund_request_owner_only(env):
    c, pp, L = env
    a, pid = bought(c, L)
    other = agent(c, L)
    for h in ({"X-Agent-Key": a["api_key"]}, {"X-Agent-Key": other["api_key"]}, {}, {"X-Admin-Token": "wrong"}):
        assert refund(c, pid, headers=h).status_code == 401
    assert L.purchase(pid)["status"] == "completed"
    assert refund(c, pid).status_code == 200                                      # the owner can


def test_second_refund_request_409(env):
    c, pp, L = env
    a, pid = bought(c, L)
    assert refund(c, pid).status_code == 200
    r = refund(c, pid)
    assert r.status_code == 409 and L.purchase(pid)["status"] == "refund_requested"


@pytest.mark.parametrize("status", ["payment_pending", "payment_unknown", "payment_failed", "flagged", "failed",
                                    "pending_approval", "refunded", "partially_refunded", "completing"])
def test_refund_request_only_from_completed(env, status):
    c, pp, L = env
    a, pid = bought(c, L)
    L.update_purchase(pid, status=status)
    r = refund(c, pid)
    assert r.status_code == 409 and f"purchase is {status}" in r.text and L.purchase(pid)["status"] == status


def test_refund_request_live_data_non_refundable(env, monkeypatch):
    c, pp, L = env
    a = agent(c, L)
    live(monkeypatch, lambda *x, **k: {"ok": 1})
    pid = buy(c, a).json()["purchase_id"]
    r = refund(c, pid)
    assert r.status_code == 409 and "non-refundable" in r.text and L.purchase(pid)["status"] == "completed"


def test_refund_request_window_closed(env):
    c, pp, L = env
    a, pid = bought(c, L)
    L.update_purchase(pid, refund_deadline=time.time() - 1)
    r = refund(c, pid)
    assert r.status_code == 409 and "window" in r.text and L.purchase(pid)["status"] == "completed"


def test_refund_request_flag_off_503(tmp_path, ids):
    c, pp, L = make(tmp_path)
    a, pid = bought(c, L)
    c2, pp2, _ = make(tmp_path, vendor_payouts=False)
    r = refund(c2, pid)
    assert r.status_code == 503 and L.purchase(pid)["status"] == "completed"


def test_refund_request_cannot_simulate_a_decline(env):
    c, pp, L = env
    a, pid = bought(c, L)
    r = refund(c, pid, headers=A | {"X-Simulate-Decline": "INSTRUMENT_DECLINED"})
    assert r.status_code == 422 and L.purchase(pid)["status"] == "completed"


def test_listing_shows_refund_with_the_vendor(env):
    c, pp, L = env
    a, pid = bought(c, L)
    refund(c, pid)
    for r in (c.get("/api/me/purchases", headers={"X-Agent-Key": a["api_key"]}),
              c.get(f"/api/purchases?agent_id={a['agent_id']}", headers=A)):
        [row] = [p for p in r.json() if p["id"] == pid]
        assert row["paid_to"] == "SampleBox Wholesale"
        assert row["refund"]["status"] == "requested" and row["refund"]["with"] == "SampleBox Wholesale"
        assert "with SampleBox Wholesale" in row["refund"]["note"]
        no_ids_in(r.text)
        assert "pay_vault_id" not in r.text


# ================= reconcile: payment_unknown =================
def test_reconcile_replays_unknown_with_same_request_id(env):
    c, pp, L = env
    a, pid = unknown(c, pp, L)
    n = len(pays(pp))
    r = reconcile(c, a)
    assert r.status_code == 200, r.text
    new = pays(pp)[n:]
    assert [q["request_id"] for q in new] == [f"at-{pid}-pay"]
    assert new[0]["body"] == pays(pp)[0]["body"]                                  # the same order, replayed
    assert vrows(r)[pid]["status_after"] == "completed"
    pu = L.purchase(pid)
    assert pu["status"] == "completed" and pu["paypal_capture"] and pu["refund_deadline"]
    assert L.agent(a["agent_id"])["frozen_reason"]                                 # reconcile never unfreezes
    no_ids_in(r.text)


def test_replay_returns_the_first_order_never_a_second(env):
    c, pp, L = env
    a, pid = unknown(c, pp, L, script=("timeout_after", "timeout_after"))        # PayPal did make the order
    assert len(pp.mock_vendor_orders) == 1
    r = reconcile(c, a)
    assert vrows(r)[pid]["status_after"] == "completed" and len(pp.mock_vendor_orders) == 1


def test_replay_definite_refusal_payment_failed(env):
    c, pp, L = env
    a, pid = unknown(c, pp, L)
    pp.mock_vendor_script = [("error", 422, "INSTRUMENT_DECLINED")]
    r = reconcile(c, a)
    assert vrows(r)[pid]["status_after"] == "payment_failed"
    assert L.purchase(pid)["status"] == "payment_failed" and counted(L, a["agent_id"]) == 0


def test_replay_still_unknown_stays_unknown(env):
    c, pp, L = env
    a, pid = unknown(c, pp, L)
    pp.mock_vendor_script = ["timeout_before"]
    r = reconcile(c, a)
    assert r.json()["unverified"] >= 1 and L.purchase(pid)["status"] == "payment_unknown"
    assert counted(L, a["agent_id"]) == 2400 and len([q for q in pays(pp) if q["request_id"] == f"at-{pid}-pay"]) == 3


def test_replay_mismatch_flagged(env):
    c, pp, L = env
    a, pid = unknown(c, pp, L)
    pp.mock_vendor_tamper = lambda j: j["purchase_units"][0].update(payee={"merchant_id": "OTHERMERCH001"})
    r = reconcile(c, a)
    assert vrows(r)[pid]["status_after"] == "flagged" and not r.json()["ok"]
    assert L.purchase(pid)["status"] == "flagged" and counted(L, a["agent_id"]) == 2400


def test_no_replay_after_the_request_id_window(env):
    c, pp, L = env
    a, pid = unknown(c, pp, L)
    L.update_purchase(pid, pay_attempt_ts=time.time() - 5.5 * H)
    n = len(pays(pp))
    r = reconcile(c, a)
    assert len(pays(pp)) == n                                                      # never risk a second payment
    assert L.purchase(pid)["status"] == "payment_unknown" and counted(L, a["agent_id"]) == 2400
    assert not r.json()["ok"] and any(pid in m and "replay" in m for m in r.json()["mismatches"])
    assert L.agent(a["agent_id"])["frozen_reason"]


def test_first_attempt_time_recorded_and_kept_by_replay(env):
    c, pp, L = env
    a, pid = unknown(c, pp, L)
    t0 = L.purchase(pid)["pay_attempt_ts"]
    assert t0 and time.time() - t0 < 60
    pp.mock_vendor_script = ["timeout_before"]
    reconcile(c, a)
    assert L.purchase(pid)["pay_attempt_ts"] == t0


# ================= unfreeze rules =================
def test_unfreeze_refused_while_reconcile_can_still_decide(env):
    c, pp, L = env
    a, pid = unknown(c, pp, L)
    r = c.post(f"/api/agents/{a['agent_id']}/unfreeze", headers=A)
    assert r.status_code == 409 and "reconcile" in r.text
    assert L.agent(a["agent_id"])["frozen_reason"]
    reconcile(c, a)
    assert L.purchase(pid)["status"] == "completed"
    assert c.post(f"/api/agents/{a['agent_id']}/unfreeze", headers=A).status_code == 200
    assert not L.agent(a["agent_id"])["frozen_reason"]


def test_unfreeze_allowed_when_only_the_owner_can_decide(env):
    c, pp, L = env
    a, pid = unknown(c, pp, L)
    L.update_purchase(pid, pay_attempt_ts=time.time() - 6 * H)                     # too old to replay
    assert c.post(f"/api/agents/{a['agent_id']}/unfreeze", headers=A).status_code == 200


def test_unfreeze_refused_with_stale_half_done_row(env):
    c, pp, L = env
    a, pid = bought(c, L)
    L.update_purchase(pid, status="completing", started_ts=time.time() - 3600)
    L.freeze(a["agent_id"], "x")
    assert c.post(f"/api/agents/{a['agent_id']}/unfreeze", headers=A).status_code == 409


# ================= reconcile: payment_pending =================
def _stored(pp, pid):
    return pp.mock_vendor_orders[f"at-{pid}-pay"]


def test_pending_becomes_completed_when_paypal_completes(env):
    c, pp, L = env
    a = agent(c, L)
    pp.mock_vendor_tamper = lambda j: j["purchase_units"][0]["payments"]["captures"][0].update(status="PENDING")
    pid = buy(c, a).json()["purchase_id"]
    pp.mock_vendor_tamper = None
    n = len(pays(pp))
    r = reconcile(c, a)
    assert len(pays(pp)) == n                                                      # read, never re-created
    assert vrows(r)[pid]["status_after"] == "completed" and L.purchase(pid)["refund_deadline"]


def test_pending_still_pending_unchanged(env):
    c, pp, L = env
    a = agent(c, L)
    pp.mock_vendor_tamper = lambda j: j["purchase_units"][0]["payments"]["captures"][0].update(status="PENDING")
    pid = buy(c, a).json()["purchase_id"]
    pp.mock_vendor_tamper = None
    _stored(pp, pid)["purchase_units"][0]["payments"]["captures"][0]["status"] = "PENDING"
    r = reconcile(c, a)
    assert L.purchase(pid)["status"] == "payment_pending" and r.json()["ok"]
    assert vrows(r)[pid]["status_after"] == "payment_pending" and vrows(r)[pid]["paypal_status"] == "PENDING"


def test_pending_order_mismatch_flagged(env):
    c, pp, L = env
    a = agent(c, L)
    pp.mock_vendor_tamper = lambda j: j["purchase_units"][0]["payments"]["captures"][0].update(status="PENDING")
    pid = buy(c, a).json()["purchase_id"]
    pp.mock_vendor_tamper = None
    _stored(pp, pid)["purchase_units"][0]["amount"]["value"] = "99.00"
    r = reconcile(c, a)
    assert L.purchase(pid)["status"] == "flagged" and not r.json()["ok"] and L.agent(a["agent_id"])["frozen_reason"]


# ================= reconcile: captures (refunds, mismatches) =================
def _cap(pp, L, pid):
    return pp._mock_captures[L.purchase(pid)["paypal_capture"]]


def test_refunded_capture_becomes_refunded_and_stops_counting(env):
    c, pp, L = env
    a, pid = bought(c, L)
    refund(c, pid)
    _cap(pp, L, pid)["status"] = "REFUNDED"
    r = reconcile(c, a)
    assert r.json()["ok"] and vrows(r)[pid]["status_after"] == "refunded"
    pu = L.purchase(pid)
    assert pu["status"] == "refunded" and pu["refunded_ts"] and counted(L, a["agent_id"]) == 0
    assert not L.agent(a["agent_id"])["frozen_reason"] and money_entries(L, a["agent_id"]) == []


def test_vendor_refunded_without_a_request(env):
    c, pp, L = env
    a, pid = bought(c, L)
    _cap(pp, L, pid)["status"] = "REFUNDED"
    reconcile(c, a)
    assert L.purchase(pid)["status"] == "refunded" and counted(L, a["agent_id"]) == 0


def test_partial_refund_keeps_counting_then_full(env):
    c, pp, L = env
    a, pid = bought(c, L)
    refund(c, pid)
    _cap(pp, L, pid)["status"] = "PARTIALLY_REFUNDED"
    reconcile(c, a)
    assert L.purchase(pid)["status"] == "partially_refunded" and counted(L, a["agent_id"]) == 2400
    _cap(pp, L, pid)["status"] = "REFUNDED"
    reconcile(c, a)
    assert L.purchase(pid)["status"] == "refunded" and counted(L, a["agent_id"]) == 0


@pytest.mark.parametrize("field,value", [("amount", "25.00"), ("payee", "OTHERMERCH001"), ("status", "REVERSED"),
                                         ("status", "DECLINED"), ("status", "FAILED")])
def test_capture_mismatch_freezes(env, field, value):
    from decimal import Decimal
    c, pp, L = env
    a, pid = bought(c, L)
    _cap(pp, L, pid)[field] = Decimal(value) if field == "amount" else value
    r = reconcile(c, a)
    assert not r.json()["ok"] and any(pid in m for m in r.json()["mismatches"])
    assert L.agent(a["agent_id"])["frozen_reason"] and counted(L, a["agent_id"]) == 2400
    assert L.purchase(pid)["status"] == "completed"
    no_ids_in(r.text)


def test_capture_not_found_is_a_mismatch(env):
    c, pp, L = env
    a, pid = bought(c, L)
    del pp._mock_captures[L.purchase(pid)["paypal_capture"]]
    r = reconcile(c, a)
    assert not r.json()["ok"] and vrows(r)[pid]["paypal_status"] == "NOT_FOUND"
    assert L.agent(a["agent_id"])["frozen_reason"]


def test_capture_lookup_unavailable_is_unverified_not_a_mismatch(env):
    c, pp, L = env
    a, pid = bought(c, L)
    pp.mock_fail["get_capture"] = "SERVICE_UNAVAILABLE"        # the mock answers 503 for a get_capture failure
    r = reconcile(c, a)
    assert r.json()["unverified"] >= 1 and r.json()["ok"] and vrows(r)[pid]["paypal_status"] == "UNVERIFIED"
    assert not L.agent(a["agent_id"])["frozen_reason"] and L.purchase(pid)["status"] == "completed"


# ================= reconcile: half-done rows =================
def test_stale_reserved_row_released_without_paypal(env):
    c, pp, L = env
    a, pid = bought(c, L)
    L.update_purchase(pid, status="reserved", ts=time.time() - 3600, paypal_order="", paypal_capture="")
    n = len(pp.mock_requests)
    r = reconcile(c, a)
    assert len(pp.mock_requests) == n
    assert L.purchase(pid)["status"] == "failed" and counted(L, a["agent_id"]) == 0 and r.json()["ok"]


def test_stale_completing_without_delivery_flagged_for_owner(env):
    c, pp, L = env
    a, pid = bought(c, L)
    L.update_purchase(pid, status="completing", started_ts=time.time() - 3600, fulfillment="", paypal_order="",
                      paypal_capture="", pay_attempt_ts=None)
    n = len(pp.mock_requests)
    r = reconcile(c, a)
    assert len(pp.mock_requests) == n                                              # never pay without a delivery
    pu = L.purchase(pid)
    assert pu["status"] == "failed" and pu["flag"] == "vendor_may_have_delivered"
    assert not r.json()["ok"] and L.agent(a["agent_id"])["frozen_reason"]


def test_stale_completing_after_delivery_is_replayed(env):
    c, pp, L = env
    a = agent(c, L)
    pid, why = L.reserve_vendor_purchase(a["agent_id"], "samplebox", "linen-napkin-samples", 1, 2400, "",
                                         "linen napkin samples for the spring catalog", "reserved", MID["samplebox"],
                                         {"per_purchase_max_cents": 12000, "daily_cap_cents": 25000}, "k", "ok")
    assert pid, why
    L.update_purchase(pid, status="completing", started_ts=time.time() - 3600, fulfillment='{"ok": 1}')
    r = reconcile(c, a)
    assert [q["request_id"] for q in pays(pp)] == [f"at-{pid}-pay"]
    assert L.purchase(pid)["status"] == "completed" and vrows(r)[pid]["status_after"] == "completed"


def test_fresh_completing_row_untouched(env):
    c, pp, L = env
    a, pid = bought(c, L)
    L.update_purchase(pid, status="completing", started_ts=time.time(), fulfillment='{"ok": 1}')
    n = len(pp.mock_requests)
    reconcile(c, a)
    assert len(pp.mock_requests) == n and L.purchase(pid)["status"] == "completing"


# ================= never auto-approve, flag off, auth =================
def test_reconcile_never_approves(env):
    c, pp, L = env
    a = agent(c, L)
    pid = buy(c, a, BLANKETS).json()["purchase_id"]
    stale = buy(c, a, BLANKETS).json()["purchase_id"]
    L.update_purchase(stale, approval_expires=time.time() - 1)
    r = reconcile(c, a)
    assert pp.mock_requests == [] and r.status_code == 200
    assert L.purchase(pid)["status"] == "pending_approval" and L.purchase(stale)["status"] == "expired"


def test_reconcile_owner_only(env):
    c, pp, L = env
    a, pid = bought(c, L)
    assert c.post(f"/api/agents/{a['agent_id']}/reconcile", headers={"X-Agent-Key": a["api_key"]}).status_code == 401
    assert c.post(f"/api/agents/{a['agent_id']}/reconcile").status_code == 401
    assert pid in vrows(reconcile(c, a))


def test_reconcile_flag_off_skips_vendor_rows(tmp_path, ids):
    c, pp, L = make(tmp_path)
    pp.mock_vendor_script = ["timeout_before", "timeout_before"]
    a = agent(c, L)
    pid = buy(c, a).json()["detail"]["purchase_id"]
    c2, pp2, _ = make(tmp_path, vendor_payouts=False)
    r = c2.post(f"/api/agents/{a['agent_id']}/reconcile", headers=A)
    assert r.status_code == 200 and r.json()["vendor_purchases_skipped"] is True
    assert pp2.mock_requests == [] and L.purchase(pid)["status"] == "payment_unknown"


def test_reconcile_for_tab_agents_unchanged(env):
    c, pp, L = env
    r = c.post("/api/agents", json={"name": "t", "policy": "Only buy research data for the shop, nothing else.",
                                    "daily_cap_usd": 10.0, "per_call_cap_usd": 5.0}, headers=A).json()
    rr = c.post(f"/api/agents/{r['agent_id']}/reconcile", headers=A)
    assert rr.status_code == 200 and rr.json()["ok"] and rr.json()["vendor_purchases"] == []
