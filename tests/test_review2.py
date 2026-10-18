"""Review round 2: bounded per-agent purchase lock (429 busy) and auto top-up as authorize -> commit -> capture."""
import threading
import time
from decimal import Decimal

import pytest

from app import procurement as pr
from tests.test_agenttab import A, buy, shop_agent
from tests.test_hardening import _client

MUG = dict(vendor="samplebox", item="ceramic-mug-sample")
main_mod = __import__("app.main", fromlist=["TOOLS"])


# ---------- (B) per-agent lock: bounded wait, 429 busy ----------
def _slow_guard(monkeypatch, seconds):
    real = pr.decide
    gate = threading.Event()

    def slow(*a, **k):
        gate.set()
        time.sleep(seconds)
        return real(*a, **k)
    monkeypatch.setattr(pr, "decide", slow)
    return gate


def test_second_purchase_waits_briefly_then_gets_429_busy(tmp_path, monkeypatch):
    c = _client(tmp_path, agent_lock_timeout_s=0.3)
    ag = shop_agent(c, daily_cap_usd=500.0, vendor_budgets={"samplebox": 500.0})
    c.post(f"/api/agents/{ag['agent_id']}/topup-sandbox-card", json={"amount_usd": 200}, headers=A)
    gate = _slow_guard(monkeypatch, 1.2)
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("first", buy(c, ag["api_key"], **MUG)))
    t.start(); gate.wait(5)
    t0 = time.time()
    r = buy(c, ag["api_key"], **MUG)
    waited = time.time() - t0
    t.join()
    assert r.status_code == 429 and r.headers["retry-after"] == "2" and "in progress" in r.json()["detail"]
    assert 0.25 <= waited < 1.0                                   # bounded wait, not the whole guard call
    assert out["first"].status_code == 200
    led = c.app.state.ledger
    assert [p["status"] for p in led.purchases(ag["agent_id"])] == ["completed"]   # the busy one wrote nothing


def test_busy_lock_is_per_agent_and_released_after_errors(tmp_path, monkeypatch):
    c = _client(tmp_path, agent_lock_timeout_s=0.3)
    a1 = shop_agent(c, daily_cap_usd=500.0, vendor_budgets={"samplebox": 500.0})
    a2 = shop_agent(c, daily_cap_usd=500.0, vendor_budgets={"samplebox": 500.0})
    for ag in (a1, a2):
        c.post(f"/api/agents/{ag['agent_id']}/topup-sandbox-card", json={"amount_usd": 200}, headers=A)
    gate = _slow_guard(monkeypatch, 1.0)
    t = threading.Thread(target=lambda: buy(c, a1["api_key"], **MUG)); t.start(); gate.wait(5)
    assert buy(c, a2["api_key"], **MUG).status_code == 200           # another agent is never blocked
    t.join()
    # a guard block (403) and a vendor failure (502) inside the lock release it
    assert buy(c, a1["api_key"], vendor="giftcards-now", item="giftcard-25", purpose="gift").status_code == 403
    monkeypatch.setitem(pr.VENDORS["samplebox"], "fulfillment", "live")
    monkeypatch.setitem(main_mod.TOOLS, "ceramic-mug-sample", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert buy(c, a1["api_key"], **MUG).status_code == 502
    monkeypatch.setitem(pr.VENDORS["samplebox"], "fulfillment", "simulated")
    assert buy(c, a1["api_key"], **MUG).status_code == 200


def test_mcp_buy_reports_busy_instead_of_hanging(tmp_path, monkeypatch):
    c = _client(tmp_path, agent_lock_timeout_s=0.3)
    ag = shop_agent(c, daily_cap_usd=500.0, vendor_budgets={"samplebox": 500.0})
    c.post(f"/api/agents/{ag['agent_id']}/topup-sandbox-card", json={"amount_usd": 200}, headers=A)
    gate = _slow_guard(monkeypatch, 1.2)
    t = threading.Thread(target=lambda: buy(c, ag["api_key"], **MUG)); t.start(); gate.wait(5)
    v = "2025-11-25"
    h = {"X-Agent-Key": ag["api_key"], "MCP-Protocol-Version": v, "Accept": "application/json, text/event-stream"}
    r = c.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "buy", "arguments": {"vendor": "samplebox", "item": "ceramic-mug-sample", "purpose": "Evaluate supplier samples"}}})
    t.join()
    res = r.json()["result"]
    assert res["isError"] and "rate_limited" in str(res) and "in progress" in str(res)


# ---------- (C) auto top-up: authorize, deliver, commit, then capture ----------
def _auto(tmp_path, **kw):
    c = _client(tmp_path, **kw)
    ag = shop_agent(c, daily_cap_usd=500.0, vendor_budgets={"samplebox": 500.0})
    aid = ag["agent_id"]
    assert c.post(f"/api/agents/{aid}/saved-card", headers=A).status_code == 200
    assert c.put(f"/api/agents/{aid}/auto-topup", json={"amount_usd": 100, "monthly_cap_usd": 300}, headers=A).status_code == 200
    return c, ag, aid


def _live_mug(monkeypatch, fn):
    monkeypatch.setitem(pr.VENDORS["samplebox"], "fulfillment", "live")
    monkeypatch.setitem(main_mod.TOOLS, "ceramic-mug-sample", fn)


def test_auto_topup_card_is_only_held_during_delivery_and_captured_after_commit(tmp_path, monkeypatch):
    c, ag, aid = _auto(tmp_path)
    pp, led = c.app.state.paypal, c.app.state.ledger
    seen = {}

    def deliver(*a, **k):        # while the vendor delivers: a hold exists, nothing captured, no credit yet
        seen["auths"] = [x["status"] for x in pp._mock_auths.values()]
        seen["captures"] = len(pp._mock_captures)
        seen["balance"] = led.balance(aid)
        return {"ok": True}
    _live_mug(monkeypatch, deliver)
    caps_before = len(pp._mock_captures)
    r = buy(c, ag["api_key"], **MUG)
    assert r.status_code == 200, r.text
    j = r.json()
    assert seen == {"auths": ["CREATED"], "captures": caps_before, "balance": Decimal(0)}
    assert j["auto_topup"]["status"] == "captured" and j["auto_topup"]["amount_usd"] == 100.0
    cid = j["auto_topup"]["paypal_capture_id"]
    assert list(pp._mock_auths.values())[0]["status"] == "CAPTURED" and cid in pp._mock_captures
    fund = [e for e in led.entries(aid) if e["kind"] == "fund"]
    assert len(fund) == 1 and fund[0]["paypal_ref"] == cid and fund[0]["tool"] == "vault-auto"
    assert j["balance_usd"] == pytest.approx(82.0) and led.balance(aid) == Decimal("82.00")
    rec = c.post(f"/api/agents/{aid}/reconcile", headers=A).json()
    assert rec["ok"] and not rec["frozen"]


def test_auto_topup_vendor_failure_voids_hold(tmp_path, monkeypatch):
    c, ag, aid = _auto(tmp_path)
    pp, led = c.app.state.paypal, c.app.state.ledger
    _live_mug(monkeypatch, lambda *a, **k: (_ for _ in ()).throw(RuntimeError("vendor down")))
    r = buy(c, ag["api_key"], **MUG)
    assert r.status_code == 502
    assert [x["status"] for x in pp._mock_auths.values()] == ["VOIDED"]
    assert led.balance(aid) == 0 and not [e for e in led.entries(aid) if e["kind"] == "fund"]
    assert led.vault_topups_since(aid, 0) == 0


def test_auto_topup_commit_conflict_409_voids_hold_and_writes_no_credit(tmp_path, monkeypatch):
    c, ag, aid = _auto(tmp_path)
    pp, led = c.app.state.paypal, c.app.state.ledger

    def deliver_while_daily_cap_is_used_up(*a, **k):   # a concurrent writer uses up the daily cap mid-delivery
        led.commit_tab_purchase(aid, "samplebox", "ceramic-mug-sample", 1, Decimal("0"), "", "x", None, "t", "t",
                                "{}", "t", {"daily_cap": Decimal("99"), "vendor_budget": None})
        led.db.execute("UPDATE purchases SET amount_micro=? WHERE agent_id=?", (500_000_000, aid)); led.db.commit()
        return {"ok": True}
    _live_mug(monkeypatch, deliver_while_daily_cap_is_used_up)
    r = buy(c, ag["api_key"], **MUG)
    assert r.status_code == 409 and "daily cap" in r.json()["detail"]
    assert [x["status"] for x in pp._mock_auths.values()] == ["VOIDED"]
    assert not [e for e in led.entries(aid) if e["kind"] == "fund"] and led.vault_topups_since(aid, 0) == 0


def test_auto_topup_capture_declined_after_delivery_reverses_credit_and_freezes(tmp_path):
    c, ag, aid = _auto(tmp_path)
    pp, led = c.app.state.paypal, c.app.state.ledger
    pp.mock_fail["capture_authorization"] = "TRANSACTION_REFUSED"
    r = buy(c, ag["api_key"], **MUG)
    assert r.status_code == 200
    j = r.json()
    assert j["auto_topup"]["status"] == "capture_declined" and j["auto_topup"]["issue"] == "TRANSACTION_REFUSED"
    kinds = sorted(e["kind"] for e in led.entries(aid) if e["kind"] in ("fund", "charge", "fund_reversal"))
    assert kinds == ["charge", "fund", "fund_reversal"]
    assert led.balance(aid) == Decimal("-18.00") and j["balance_usd"] == pytest.approx(-18.0)
    a = led.agent(aid)
    assert a["frozen_reason"] and "TRANSACTION_REFUSED" in a["frozen_reason"]
    assert led.purchase(j["purchase_id"])["flag"] == "topup_capture_declined"
    assert [x["status"] for x in pp._mock_auths.values()] == ["VOIDED"]       # the declined hold is released
    assert buy(c, ag["api_key"], **MUG).status_code == 423
    v = c.get("/api/agents", headers=A).json()[0]
    assert v["frozen"] and v["balance_usd"] == pytest.approx(-18.0)


def test_auto_topup_declined_authorization_is_402_and_nothing_held(tmp_path):
    c, ag, aid = _auto(tmp_path)
    pp, led = c.app.state.paypal, c.app.state.ledger
    pp.mock_fail["authorize_vault"] = "INSTRUMENT_DECLINED"
    r = buy(c, ag["api_key"], **MUG)
    assert r.status_code == 402 and "INSTRUMENT_DECLINED" in r.json()["auto_topup"]
    assert pp._mock_auths == {} and led.balance(aid) == 0 and led.purchases(aid) == []


# ---------- (A) eval data: no real gambling brand in cases, results or the homepage replay feed ----------
def test_eval_and_replay_feed_use_only_a_fictional_casino_domain(tmp_path):
    import json
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    for f in ("eval/cases.json", "eval/results.json", "README.md", "docs/judge-instructions.md"):
        assert "stake.com" not in (root / f).read_text(encoding="utf-8").lower(), f
    cases = {c["id"]: c for c in json.loads((root / "eval/cases.json").read_text(encoding="utf-8"))}
    assert cases["personal_use-05"]["target"] == "https://www.lucky-spin-casino.example"
    assert cases["indirect_injection-05"]["target"] == "https://www.lucky-spin-casino.example"
    c = _client(tmp_path)
    feed = c.get("/api/replay").text.lower()
    assert "stake.com" not in feed and "lucky-spin-casino.example" in feed
    res = json.loads((root / "eval/results.json").read_text(encoding="utf-8"))["modes"]["ai"]
    rows = {r["id"]: r for r in res["rows"]}
    assert rows["indirect_injection-05"]["layer"] == "policy-ai"        # regenerated by a real Gemini run
    assert res["attacks_stopped"] == res["attacks_total"] == 47 and res["false_blocks"] == 0
