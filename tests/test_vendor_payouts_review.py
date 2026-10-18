"""Vendor payouts, step 4 review findings (reviewer, Oct 18 @ e240712), each test fails without its fix.
(1) 401 = our OAuth token expired: refresh and retry once with the same Request-Id; a second 401 -> payment_failed.
(2) MCP buy outcomes: card_required, payment_failed, payment_unknown, payment_flagged (never payment_required or a
    generic error, and never "not charged" when PayPal may have charged).
(3) Owner-only resolve-unknown for payment_unknown rows past the replay window: a verified capture id, or "not paid".
(4) Partial refunds: subtract the refunded amount when PayPal's order shows it; otherwise the full amount counts.
Mock PayPal only; no network, no AI provider, no live site."""
from __future__ import annotations

import json
import time
from decimal import Decimal

import pytest

from app import vendorpay
from app.paypal import PayPalError
from test_vendor_payouts_flow import (A, MID, NAPKINS, agent, buy, counted, env, ids, make, money_entries,  # noqa: F401
                                      no_ids_in, pays)

H = 3600
MOD = "2026-07-28"
AUTH = ("error", 401, "AUTHENTICATION_FAILURE")


def unknown(c, pp, L, age_h=6.0):
    a = agent(c, L)
    pp.mock_vendor_script = ["timeout_before", "timeout_before"]
    r = buy(c, a)
    assert r.status_code == 502 and r.json()["detail"]["error"] == "payment_unknown", r.text
    pid = r.json()["detail"]["purchase_id"]
    L.update_purchase(pid, pay_attempt_ts=time.time() - age_h * H)
    return a, pid


def bought(c, L, **kw):
    a = agent(c, L, **kw)
    r = buy(c, a)
    assert r.status_code == 200, r.text
    return a, r.json()["purchase_id"]


def reconcile(c, a):
    return c.post(f"/api/agents/{a['agent_id']}/reconcile", headers=A)


def rpc(c, key, tool, args):
    params = {"name": tool, "arguments": args,
              "_meta": {"io.modelcontextprotocol/protocolVersion": MOD,
                        "io.modelcontextprotocol/clientInfo": {"name": "pytest", "version": "1"}}}
    h = {"X-Agent-Key": key, "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": MOD,
         "Mcp-Method": "tools/call", "Mcp-Name": tool}
    r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()["result"]


def mcp_buy(c, a, body=NAPKINS):
    return rpc(c, a["api_key"], "buy", body)


# ================= (1) 401: refresh the token, retry once with the same Request-Id =================
def test_401_refreshes_token_and_retries_with_the_same_request_id(env):
    c, pp, L = env
    a = agent(c, L)
    pp.mock_vendor_script = [AUTH, "ok"]
    r = buy(c, a)
    assert r.status_code == 200, r.text
    pid = r.json()["purchase_id"]
    assert [q["request_id"] for q in pays(pp)] == [f"at-{pid}-pay"] * 2
    assert pp.mock_token_resets == 1                        # a fresh token, not the expired one
    assert L.purchase(pid)["status"] == "completed" and not L.agent(a["agent_id"])["frozen_reason"]


def test_second_401_is_payment_failed_and_freezes(env):
    c, pp, L = env
    a = agent(c, L)
    pp.mock_vendor_script = [AUTH, AUTH, "ok"]
    r = buy(c, a)
    assert r.status_code == 402, r.text
    d = r.json()["detail"]
    assert d["error"] == "payment_failed"
    pid = d["purchase_id"]
    assert len(pays(pp)) == 2                               # exactly one retry, never a third call
    assert pp.mock_token_resets == 1
    assert L.purchase(pid)["status"] == "payment_failed" and counted(L, a["agent_id"]) == 0
    assert pid in L.agent(a["agent_id"])["frozen_reason"]


def test_401_then_timeout_is_unknown_not_failed(env):
    """After the token refresh the answer is lost: unknown (one more retry with the same id), never payment_failed."""
    c, pp, L = env
    a = agent(c, L)
    pp.mock_vendor_script = [AUTH, "timeout_before", "ok"]
    r = buy(c, a)
    assert r.status_code == 200, r.text
    pid = r.json()["purchase_id"]
    assert {q["request_id"] for q in pays(pp)} == {f"at-{pid}-pay"} and L.purchase(pid)["status"] == "completed"


def test_reconcile_replay_401_refreshes_too(env):
    c, pp, L = env
    a, pid = unknown(c, pp, L, age_h=1)
    pp.mock_vendor_script = [AUTH, "ok"]
    r = reconcile(c, a)
    assert r.status_code == 200, r.text
    assert L.purchase(pid)["status"] == "completed" and pp.mock_token_resets == 1


def test_one_401_is_not_a_definite_refusal_on_its_own():
    """The classifier: a single 401 never means 'card refused'. Only the client's retried 401 does."""
    assert not vendorpay.definite_refusal(PayPalError("x", "AUTHENTICATION_FAILURE", 401))
    e = PayPalError("x", "AUTHENTICATION_FAILURE", 401)
    e.auth_retried = True
    assert vendorpay.definite_refusal(e)
    for st in (400, 403, 404, 422):
        assert vendorpay.definite_refusal(PayPalError("x", "I", st))


def test_real_client_drops_the_token_on_401(monkeypatch, tmp_path):
    """Sandbox client (no network): a 401 makes the next request fetch a new OAuth token, same Request-Id."""
    import httpx
    from app.config import Settings
    from app.paypal import PayPalClient
    s = Settings(paypal_mode="sandbox", paypal_base="https://api-m.sandbox.paypal.com", paypal_client_id="id",
                 paypal_client_secret="secret", db_path=str(tmp_path / "x.db"))
    pp = PayPalClient(s)
    tokens, seen = iter(["T1", "T2"]), []

    def post(url, **kw):
        return httpx.Response(200, json={"access_token": next(tokens), "expires_in": 3000})

    def request(method, url, json=None, headers=None, timeout=None):
        seen.append((headers["Authorization"], headers["PayPal-Request-Id"]))
        return httpx.Response(401 if len(seen) == 1 else 200, json={"name": "AUTHENTICATION_FAILURE"} if len(seen) == 1
                              else {"id": "O1", "status": "COMPLETED", "purchase_units": []})
    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(httpx, "request", request)
    out = pp.pay_vendor("VAULT1", 2400, "SBXMERCHANT01", "pu_0123456789ab", "x")
    assert out["id"] == "O1"
    assert seen == [("Bearer T1", "at-pu_0123456789ab-pay"), ("Bearer T2", "at-pu_0123456789ab-pay")]


# ================= (2) MCP buy outcomes =================
def test_mcp_card_required_is_its_own_outcome(env):
    c, pp, L = env
    a = agent(c, L, card=False)
    res = mcp_buy(c, a)
    sc = res["structuredContent"]
    assert sc["outcome"] == "card_required", sc
    assert "payment_required" not in json.dumps(sc) and "request_topup" not in json.dumps(sc)
    assert "saved card" in sc["explanation"] and "owner" in sc["explanation"] and "Nothing was bought" in sc["explanation"]
    assert pays(pp) == []


def test_mcp_payment_failed_outcome(env):
    c, pp, L = env
    a = agent(c, L)
    pp.mock_vendor_script = [("error", 422, "INSTRUMENT_DECLINED")]
    res = mcp_buy(c, a)
    sc = res["structuredContent"]
    assert sc["outcome"] == "payment_failed" and res["isError"] is True, sc
    assert "refused" in sc["explanation"] and "frozen" in sc["explanation"]
    no_ids_in(json.dumps(res))


def test_mcp_payment_unknown_never_says_not_charged(env):
    c, pp, L = env
    a = agent(c, L)
    pp.mock_vendor_script = ["timeout_before", "timeout_before"]
    sc = mcp_buy(c, a)["structuredContent"]
    assert sc["outcome"] == "payment_unknown", sc
    assert "not_charged" not in sc["outcome"] and "may" in sc["explanation"]


def test_mcp_payment_flagged_outcome(env):
    c, pp, L = env
    a = agent(c, L)
    pp.mock_vendor_tamper = lambda o: o["purchase_units"][0]["amount"].update(value="99.00")
    sc = mcp_buy(c, a)["structuredContent"]
    assert sc["outcome"] == "payment_flagged", sc


def test_mcp_vendor_bought_and_approval(env):
    c, pp, L = env
    a = agent(c, L)
    sc = mcp_buy(c, a)["structuredContent"]
    assert sc["outcome"] == "bought" and sc["paid_to"] == "SampleBox Wholesale", sc
    big = dict(NAPKINS, quantity=3)
    sc = mcp_buy(c, a, big)["structuredContent"]
    assert sc["outcome"] == "approval_required", sc
    assert "owner" in sc["explanation"] and "Nothing is charged" in sc["explanation"]


def test_mcp_vendor_flag_off_outcome(tmp_path, ids):
    c, pp, L = make(tmp_path, vendor_payouts=False)
    a = agent(c, L, card=False)
    sc = mcp_buy(c, a)["structuredContent"]
    assert sc["outcome"] == "unavailable", sc


def test_mcp_instructions_name_the_vendor_outcomes(env):
    c, pp, L = env
    mcp = c.app.state.mcp
    for word in ("card_required", "payment_failed", "payment_unknown", "payment_flagged"):
        assert word in mcp.instructions


def test_mcp_list_purchases_says_refund_is_with_the_vendor(env):
    c, pp, L = env
    a, pid = bought(c, L)
    assert c.post(f"/api/purchases/{pid}/refund", headers=A).status_code == 200
    rows = rpc(c, a["api_key"], "list_purchases", {})["structuredContent"]["purchases"]
    row = next(p for p in rows if p["id"] == pid)
    assert row["paid_to"] == "SampleBox Wholesale" and row["refund"]["with"] == "SampleBox Wholesale"
    no_ids_in(json.dumps(rows))


# ================= (3) owner-only resolve-unknown =================
def resolve(c, pid, body, headers=A):
    return c.post(f"/api/purchases/{pid}/resolve-unknown", json=body, headers=headers)


def capture(pp, cid="CAPRESOLVE01", pid="", amount="24.00", payee=MID["samplebox"], status="COMPLETED", **kw):
    d = {"amount": Decimal(amount), "refunded": Decimal(0), "status": status, "custom_id": pid,
         "order_id": "MOCK-RESOLVED01", **kw}
    if payee is not None:
        d["payee"] = payee
    pp._mock_captures[cid] = d
    return cid


def test_resolve_with_a_verified_capture_completes(env):
    c, pp, L = env
    a, pid = unknown(c, pp, L)
    cid = capture(pp, pid=pid)
    n = len(pays(pp))
    r = resolve(c, pid, {"capture_id": cid})
    assert r.status_code == 200, r.text
    pu = L.purchase(pid)
    assert pu["status"] == "completed" and pu["paypal_capture"] == cid and pu["refund_deadline"]
    assert pu["paypal_order"] == "MOCK-RESOLVED01"
    assert counted(L, a["agent_id"]) == 2400 and len(pays(pp)) == n       # no new payment
    assert L.agent(a["agent_id"])["frozen_reason"]                        # never unfreezes by itself
    assert c.post(f"/api/agents/{a['agent_id']}/unfreeze", headers=A).status_code == 200
    no_ids_in(r.text)


@pytest.mark.parametrize("change", ["payee", "amount", "currency", "custom_id", "status_pending", "status_refunded",
                                    "status_declined", "no_payee", "not_found"])
def test_resolve_refuses_a_capture_that_does_not_match(env, change):
    c, pp, L = env
    a, pid = unknown(c, pp, L)
    kw = {"pid": pid}
    if change == "payee":
        kw["payee"] = "OTHERMERCH001"
    elif change == "amount":
        kw["amount"] = "23.99"
    elif change == "currency":
        kw["currency"] = "EUR"
    elif change == "custom_id":
        kw["pid"] = "pu_aaaaaaaaaaaa"
    elif change == "status_pending":
        kw["status"] = "PENDING"
    elif change == "status_refunded":
        kw["status"] = "REFUNDED"
    elif change == "status_declined":
        kw["status"] = "DECLINED"
    elif change == "no_payee":
        kw["payee"] = None
    cid = "CAPMISSING01" if change == "not_found" else capture(pp, **kw)
    before = dict(L.purchase(pid))
    r = resolve(c, pid, {"capture_id": cid})
    assert r.status_code == 409, r.text
    assert dict(L.purchase(pid)) == before                              # state unchanged
    assert counted(L, a["agent_id"]) == 2400
    no_ids_in(r.text)


def test_resolve_refuses_a_capture_already_used_by_another_purchase(env):
    c, pp, L = env
    a2, other = bought(c, L)
    a, pid = unknown(c, pp, L)
    cap = pp._mock_captures[L.purchase(other)["paypal_capture"]]
    cap["custom_id"] = pid
    r = resolve(c, pid, {"capture_id": L.purchase(other)["paypal_capture"]})
    assert r.status_code == 409 and L.purchase(pid)["status"] == "payment_unknown"


def test_resolve_bad_capture_id_refused_without_paypal(env, monkeypatch):
    c, pp, L = env
    a, pid = unknown(c, pp, L)
    monkeypatch.setattr(pp, "get_capture", lambda cid: pytest.fail("PayPal called"))
    for bad in ("../x", "", "A" * 65, "cap id"):
        assert resolve(c, pid, {"capture_id": bad}).status_code == 422
    assert L.purchase(pid)["status"] == "payment_unknown"


def test_resolve_paypal_unavailable_changes_nothing(env):
    c, pp, L = env
    a, pid = unknown(c, pp, L)
    cid = capture(pp, pid=pid)
    pp.mock_fail["get_capture"] = "SERVICE_UNAVAILABLE"
    r = resolve(c, pid, {"capture_id": cid})
    assert r.status_code == 503 and L.purchase(pid)["status"] == "payment_unknown"


def test_resolve_not_paid_releases_and_stops_counting(env):
    c, pp, L = env
    a, pid = unknown(c, pp, L)
    n = len(pays(pp))
    r = resolve(c, pid, {"not_paid": True})
    assert r.status_code == 200, r.text
    pu = L.purchase(pid)
    assert pu["status"] == "payment_failed" and counted(L, a["agent_id"]) == 0 and len(pays(pp)) == n
    assert "owner" in (pu["reason"] or "")
    assert money_entries(L, a["agent_id"]) == []
    assert c.post(f"/api/agents/{a['agent_id']}/unfreeze", headers=A).status_code == 200


@pytest.mark.parametrize("body", [{}, {"not_paid": False}, {"capture_id": "CAPX1", "not_paid": True}])
def test_resolve_needs_exactly_one_choice(env, body):
    c, pp, L = env
    a, pid = unknown(c, pp, L)
    assert resolve(c, pid, body).status_code == 422 and L.purchase(pid)["status"] == "payment_unknown"


def test_resolve_owner_only(env):
    c, pp, L = env
    a, pid = unknown(c, pp, L)
    other = agent(c, L)
    for h in ({"X-Agent-Key": a["api_key"]}, {"X-Agent-Key": other["api_key"]}, {}, {"X-Admin-Token": "nope"}):
        assert resolve(c, pid, {"not_paid": True}, headers=h).status_code == 401
    assert L.purchase(pid)["status"] == "payment_unknown"


def test_resolve_refused_while_reconcile_can_still_replay(env):
    c, pp, L = env
    a, pid = unknown(c, pp, L, age_h=1)
    r = resolve(c, pid, {"not_paid": True})
    assert r.status_code == 409 and "reconcile" in r.text and L.purchase(pid)["status"] == "payment_unknown"


@pytest.mark.parametrize("status", ["completed", "payment_pending", "flagged", "payment_failed", "refund_requested"])
def test_resolve_only_payment_unknown(env, status):
    c, pp, L = env
    a, pid = unknown(c, pp, L)
    L.update_purchase(pid, status=status)
    r = resolve(c, pid, {"not_paid": True})
    assert r.status_code == 409 and L.purchase(pid)["status"] == status


def test_resolve_flag_off_503(tmp_path, ids):
    c, pp, L = make(tmp_path, vendor_payouts=False)
    r = c.post(f"/api/purchases/pu_0123456789ab/resolve-unknown", json={"not_paid": True}, headers=A)
    assert r.status_code == 503


# ================= (4) partial refunds: subtract what PayPal shows was refunded =================
def _order(pp, pid):
    return pp.mock_vendor_orders[f"at-{pid}-pay"]["purchase_units"][0]["payments"]


def _refunds(pp, pid, *rows):
    _order(pp, pid)["refunds"] = [{"id": f"REF{i}", "status": st, "amount": {"currency_code": cur, "value": v}}
                                  for i, (v, st, cur) in enumerate(rows)]


def _partial(pp, L, pid):
    pp._mock_captures[L.purchase(pid)["paypal_capture"]]["status"] = "PARTIALLY_REFUNDED"


def test_partial_refund_subtracts_the_refunded_amount(env):
    c, pp, L = env
    a, pid = bought(c, L)
    _partial(pp, L, pid)
    _refunds(pp, pid, ("10.00", "COMPLETED", "USD"))
    r = reconcile(c, a)
    assert r.status_code == 200 and r.json()["ok"], r.text
    pu = L.purchase(pid)
    assert pu["status"] == "partially_refunded" and pu["refunded_cents"] == 1000
    assert counted(L, a["agent_id"]) == 1400
    env_ = next(x for x in c.get("/api/agents", headers=A).json() if x["agent_id"] == a["agent_id"])["envelope"]
    assert env_["used_30d_usd"] == 14.0
    assert money_entries(L, a["agent_id"]) == []


def test_partial_refund_sums_completed_refunds_only(env):
    c, pp, L = env
    a, pid = bought(c, L)
    _partial(pp, L, pid)
    _refunds(pp, pid, ("5.00", "COMPLETED", "USD"), ("3.50", "COMPLETED", "USD"), ("4.00", "PENDING", "USD"),
             ("2.00", "FAILED", "USD"))
    reconcile(c, a)
    assert L.purchase(pid)["refunded_cents"] == 850 and counted(L, a["agent_id"]) == 1550


def test_partial_refund_amount_updates_on_the_next_reconcile(env):
    c, pp, L = env
    a, pid = bought(c, L)
    _partial(pp, L, pid)
    _refunds(pp, pid, ("5.00", "COMPLETED", "USD"))
    reconcile(c, a)
    _refunds(pp, pid, ("5.00", "COMPLETED", "USD"), ("6.00", "COMPLETED", "USD"))
    reconcile(c, a)
    assert L.purchase(pid)["refunded_cents"] == 1100 and counted(L, a["agent_id"]) == 1300


@pytest.mark.parametrize("rows", [[], [("24.00", "COMPLETED", "USD")], [("30.00", "COMPLETED", "USD")],
                                  [("5.00", "COMPLETED", "EUR")], [("5.001", "COMPLETED", "USD")],
                                  [("-5.00", "COMPLETED", "USD")]])
def test_partial_refund_without_a_usable_amount_keeps_the_full_amount(env, rows):
    c, pp, L = env
    a, pid = bought(c, L)
    _partial(pp, L, pid)
    _refunds(pp, pid, *rows)
    reconcile(c, a)
    pu = L.purchase(pid)
    assert pu["status"] == "partially_refunded" and pu["refunded_cents"] is None and counted(L, a["agent_id"]) == 2400


def test_partial_refund_order_unreadable_keeps_the_full_amount(env, monkeypatch):
    c, pp, L = env
    a, pid = bought(c, L)
    _partial(pp, L, pid)

    def boom(oid):
        raise PayPalError("GET order: 503", "UNAVAILABLE", 503)
    monkeypatch.setattr(pp, "get_order", boom)
    r = reconcile(c, a)
    assert r.status_code == 200
    assert L.purchase(pid)["status"] == "partially_refunded" and counted(L, a["agent_id"]) == 2400
    assert L.purchase(pid)["refunded_cents"] is None


def test_refunded_cents_is_not_listed_as_money_back_to_the_tab(env):
    c, pp, L = env
    a, pid = bought(c, L)
    _partial(pp, L, pid)
    _refunds(pp, pid, ("10.00", "COMPLETED", "USD"))
    reconcile(c, a)
    row = next(p for p in c.get(f"/api/purchases?agent_id={a['agent_id']}", headers=A).json() if p["id"] == pid)
    assert row["refund"]["status"] == "partial" and row["refund"]["refunded_usd"] == 10.0
    assert money_entries(L, a["agent_id"]) == []
