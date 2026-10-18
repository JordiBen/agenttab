"""Vendor payouts, step 4 UI and its backend (docs/vendor-payouts-impl-plan.md, "Top-up and balance ... budget"):
410 on every top-up/tab refund for vendor-funded agents, vendor-funded demo sessions with the flag on, the envelope
split into reserved and paid, and the dashboard's vendor statuses and wording. Static checks run without Playwright
(the browser e2e is tests/test_e2e_vendor_payouts.py). Mock PayPal only."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app import procurement as pr

from test_vendor_payouts_flow import A, BLANKETS, NAPKINS, agent, buy, env, ids, make, money_entries, no_ids_in, pays  # noqa: F401
from test_vendor_payouts_review import rpc

ROOT = Path(__file__).resolve().parent.parent
JS = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
HTML = (ROOT / "static" / "dashboard.html").read_text(encoding="utf-8")


# ================= 410 for vendor-funded agents =================
@pytest.mark.parametrize("method,path,body", [("post", "/api/agents/{aid}/topup", {"amount_usd": 10.0}),
                         ("post", "/api/agents/{aid}/topup-sandbox-card", {"amount_usd": 10.0}),
                         ("post", "/api/agents/{aid}/topup-saved-card", {"amount_usd": 10.0}),
                         ("post", "/api/agents/{aid}/refund", None),
                         ("put", "/api/agents/{aid}/auto-topup", {"amount_usd": 10.0, "monthly_cap_usd": 20.0})]
)
def test_owner_topups_410_for_vendor_agents(env, method, path, body):
    c, pp, L = env
    a = agent(c, L)
    orders, caps = len(pp._mock_orders), len(pp._mock_captures)
    r = getattr(c, method)(path.format(aid=a["agent_id"]), headers=A, **({"json": body} if body is not None else {}))
    assert r.status_code == 410, r.text
    assert "budget" in r.text and "doesn't hold money" in r.text
    assert len(pp._mock_orders) == orders and len(pp._mock_captures) == caps and money_entries(L, a["agent_id"]) == []


def test_agent_topup_request_410_for_vendor_agents(env):
    c, pp, L = env
    a = agent(c, L)
    r = c.post("/api/me/topup-request", json={"amount_usd": 10.0}, headers={"X-Agent-Key": a["api_key"]})
    assert r.status_code == 410 and len(pp._mock_orders) == 0


def test_topup_410_even_with_the_flag_off(tmp_path, ids):
    c, pp, L = make(tmp_path)
    a = agent(c, L)
    c2, pp2, L2 = make(tmp_path, vendor_payouts=False)
    assert c2.post(f"/api/agents/{a['agent_id']}/topup", json={"amount_usd": 10.0}, headers=A).status_code == 410


def test_tab_agents_still_top_up(env):
    c, pp, L = env
    r = c.post("/api/agents", json={"name": "t", "policy": pr.SHOP_POLICY, "daily_cap_usd": 50.0}, headers=A)
    assert r.status_code == 200, r.text
    aid = r.json()["agent_id"]
    assert c.post(f"/api/agents/{aid}/topup-sandbox-card", json={"amount_usd": 10.0}, headers=A).status_code == 200


def test_mcp_request_topup_for_vendor_agent(env):
    c, pp, L = env
    a = agent(c, L)
    sc = rpc(c, a["api_key"], "request_topup", {"amount_usd": 10.0})["structuredContent"]
    assert sc["outcome"] == "no_topups", sc
    assert "saved card" in sc["explanation"] and "budget" in sc["explanation"]


# ================= envelope: reserved and paid =================
def test_envelope_splits_reserved_and_paid(env):
    c, pp, L = env
    a = agent(c, L, envelope=200.0)
    assert buy(c, a).status_code == 200                       # $24 paid
    assert buy(c, a, BLANKETS).status_code == 202             # $64 waiting for approval (reserved)
    e = c.get("/api/me", headers={"X-Agent-Key": a["api_key"]}).json()["envelope"]
    assert e == {"budget_30d_usd": 200.0, "used_30d_usd": 88.0, "remaining_usd": 112.0,
                 "paid_usd": 24.0, "reserved_usd": 64.0}


# ================= vendor-funded demo sessions =================
def demo(tmp_path, **kw):
    return make(tmp_path, demo_mode=True, admin_token="demo-admin-token-not-secret-0123", **kw)


def test_demo_session_is_vendor_funded_with_the_flag_on(tmp_path, ids):
    c, pp, L = demo(tmp_path)
    r = c.post("/api/demo/session")
    assert r.status_code == 200, r.text
    j = r.json()
    a = L.agent(j["agent_id"])
    assert a["funding_model"] == "vendor" and a["envelope_cents_30d"] == 10000 and a["vault_id"]
    assert j["envelope"]["budget_30d_usd"] == 100.0 and j["funding_model"] == "vendor"
    assert "paypal_capture_id" not in j and money_entries(L, j["agent_id"]) == [] and len(pp._mock_captures) == 0
    assert "vault" not in json.dumps(j).lower()
    k = {"X-Agent-Key": j["agent_key"]}
    r = c.post("/api/purchases", json=NAPKINS, headers=k)
    assert r.status_code == 200 and r.json()["funding"] == "vendor", r.text
    pid = r.json()["purchase_id"]
    r = c.post(f"/api/purchases/{pid}/refund", headers=k)                 # the demo visitor plays the owner
    assert r.status_code == 200 and r.json()["status"] == "refund_requested"
    assert L.purchase(pid)["refund_requested_by"] == "demo-session owner"
    big = c.post("/api/purchases", json=BLANKETS, headers=k)
    assert big.status_code == 202
    assert c.post(f"/api/purchases/{big.json()['purchase_id']}/approve", headers=k).status_code == 200
    no_ids_in(json.dumps(j))


def test_demo_session_stays_tab_funded_with_the_flag_off(tmp_path, ids):
    c, pp, L = demo(tmp_path, vendor_payouts=False)
    j = c.post("/api/demo/session").json()
    assert j["balance_usd"] == 100.0 and "envelope" not in j and "funding_model" not in j


def test_demo_session_card_failure_creates_nothing_usable(tmp_path, ids):
    c, pp, L = demo(tmp_path)
    pp.mock_fail["vault"] = "PERMISSION_DENIED"
    r = c.post("/api/demo/session")
    assert r.status_code == 502 and "card" in r.text


# ================= dashboard (static, no browser) =================
def _section():
    m = re.search(r"// ---- vendor payouts ----(.*?)// ---- end vendor payouts ----", JS, re.S)
    assert m, "app.js needs a '// ---- vendor payouts ----' section"
    return m.group(1)


@pytest.mark.parametrize("status", ["reserved", "completing", "payment_pending", "payment_unknown", "payment_failed", "flagged",
                                    "refund_requested", "partially_refunded"])
def test_every_vendor_status_has_a_human_label_and_a_tip(status):
    sec = _section()
    m = re.search(rf"\b{status}: '([^']+)'", sec)
    assert m and "_" not in m.group(1), f"no label for {status}"
    assert re.search(rf"VTIP\s*=\s*\{{[^}}]*\b{status}:", sec, re.S), f"no tip for {status}"


def test_refund_requested_says_with_the_vendor():
    sec = _section()
    # the vendor moved from the pill text to its tooltip (the note names it) and the Vendor column (test_vendor_polish_ui)
    assert "pill('refund_requested', 'Refund requested', d.refund.note)" in sec and "headerName: 'Vendor'" in sec


def test_vendor_rows_use_the_vendor_actions():
    sec = _section()
    assert "'approve'" in sec and "Request refund" in sec and "resolve-unknown" in sec
    assert "approve-sandbox-card" in JS                       # tab agents keep theirs


def test_envelope_replaces_the_tab_balance():
    sec = _section()
    assert "remaining_usd" in sec and "30-day budget" in sec and "reserved_usd" in sec and "paid_usd" in sec
    assert "funding_model === 'vendor'" in JS


def test_demo_buy_handles_vendor_outcomes():
    sec = _section()
    for o in ("card_required", "payment_failed", "payment_unknown", "payment_flagged"):
        assert o in sec


def test_mcp_panel_names_the_outcomes():
    for o in ("approval_required", "payment_required", "card_required", "payment_failed", "payment_unknown",
              "payment_flagged"):
        assert o in HTML


def test_vendor_copy_rules():
    """AgentTab copy rules: no model names, no milliseconds, no internal counts or ids, no money claim yet."""
    sec = _section()
    words = " ".join(re.findall(r"'([^'\\]*(?:\\.[^'\\]*)*)'", sec) + re.findall(r"`([^`]*)`", sec))
    for bad in (r"\bms\b", r"Claude|Gemini|GPT|Anthropic|OpenAI", r"merchant", r"never touches the money",
                r"paid by PayPal only after"):
        assert not re.search(bad, words, re.I), bad
