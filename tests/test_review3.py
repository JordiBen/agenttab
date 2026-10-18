"""Review round 3: reconcile settles auto top-up credits whose hold was never captured; the schedule shows a
declined capture / cancelled hold instead of a stale 'Needs approval'."""
from concurrent.futures import CancelledError
from decimal import Decimal

from app.paypal import PayPalError
from tests.test_agenttab import A, buy, shop_agent
from tests.test_hardening import _client

MUG = dict(vendor="samplebox", item="ceramic-mug-sample")
SEAT = dict(vendor="shopsuite", item="inventory-seat", quantity=2, purpose="Inventory software to track stock across our two warehouses")


# ---------- (1) reconcile: uncaptured auto top-up older than 10 minutes ----------
def _crashed_auto_topup(tmp_path):
    """Purchase commits with an auto top-up credit, then the process 'dies' before the capture."""
    c = _client(tmp_path)
    ag = shop_agent(c, daily_cap_usd=500.0, vendor_budgets={"samplebox": 500.0})
    aid = ag["agent_id"]
    c.post(f"/api/agents/{aid}/saved-card", headers=A)
    c.put(f"/api/agents/{aid}/auto-topup", json={"amount_usd": 100, "monthly_cap_usd": 300}, headers=A)
    pp, led = c.app.state.paypal, c.app.state.ledger
    real = pp.capture_authorization
    crashed = []

    def crash(*a, **k):
        crashed.append(a)
        raise SystemExit("worker killed between commit and capture")
    pp.capture_authorization = crash
    try:
        buy(c, ag["api_key"], **MUG)
    except (SystemExit, CancelledError):
        # starlette<1.9 TestClient re-raises the SystemExit; starlette>=1.9 runs the app as a portal task, so a
        # BaseException tears the portal down and the caller sees CancelledError (starlette PR #3491).
        pass
    pp.capture_authorization = real
    assert len(crashed) == 1, "the simulated crash must have fired exactly once"
    fund = [e for e in led.entries(aid) if e["kind"] == "fund"]
    assert len(fund) == 1 and fund[0]["paypal_ref"] == "" and fund[0]["capture_ref"].startswith("AUTH-")
    assert [p["status"] for p in led.purchases(aid)] == ["completed"] and led.balance(aid) == Decimal("82.00")
    return c, aid, fund[0]


def _age(c, entry_id, seconds):
    led = c.app.state.ledger
    led.db.execute("UPDATE entries SET ts=ts-? WHERE id=?", (seconds, entry_id)); led.db.commit()


def test_reconcile_captures_a_stale_uncaptured_auto_topup_and_freezes(tmp_path):
    c, aid, fe = _crashed_auto_topup(tmp_path)
    pp, led = c.app.state.paypal, c.app.state.ledger
    _age(c, fe["id"], 601)
    r = c.post(f"/api/agents/{aid}/reconcile", headers=A).json()
    assert not r["ok"] and r["frozen"] and r["frozen_now"]
    assert any("never captured; captured now" in m for m in r["mismatches"])
    after = [e for e in led.entries(aid) if e["id"] == fe["id"]][0]
    assert after["paypal_ref"].startswith("CAP-") and pp._mock_auths[fe["capture_ref"]]["status"] == "CAPTURED"
    assert led.balance(aid) == Decimal("82.00")                      # the credit is now backed by real money
    r2 = c.post(f"/api/agents/{aid}/reconcile", headers=A).json()  # idempotent: nothing left to settle
    assert not any("never captured" in m for m in r2["mismatches"])


def test_reconcile_reverses_credit_and_voids_hold_when_paypal_refuses_capture(tmp_path):
    c, aid, fe = _crashed_auto_topup(tmp_path)
    pp, led = c.app.state.paypal, c.app.state.ledger
    _age(c, fe["id"], 601)
    pp.mock_fail["capture_authorization"] = "AUTHORIZATION_EXPIRED"
    r = c.post(f"/api/agents/{aid}/reconcile", headers=A).json()
    assert r["frozen"] and any("credit reversed" in m and "AUTHORIZATION_EXPIRED" in m for m in r["mismatches"])
    assert pp._mock_auths[fe["capture_ref"]]["status"] == "VOIDED"
    rev = [e for e in led.entries(aid) if e["kind"] == "fund_reversal"]
    assert len(rev) == 1 and rev[0]["capture_ref"] == f"entry:{fe['id']}" and rev[0]["amount_micro"] == 100_000_000
    assert led.balance(aid) == Decimal("-18.00")
    c.post(f"/api/agents/{aid}/reconcile", headers=A)              # second run must not reverse twice
    assert len([e for e in led.entries(aid) if e["kind"] == "fund_reversal"]) == 1


def test_reconcile_leaves_fresh_or_transiently_unverifiable_topups_alone(tmp_path):
    c, aid, fe = _crashed_auto_topup(tmp_path)
    pp, led = c.app.state.paypal, c.app.state.ledger
    r = c.post(f"/api/agents/{aid}/reconcile", headers=A).json()   # younger than 10 minutes: may still be capturing
    assert r["ok"] and not r["frozen"] and pp._mock_auths[fe["capture_ref"]]["status"] == "CREATED"
    _age(c, fe["id"], 601)
    real = pp.capture_authorization

    def down(*a, **k):
        raise PayPalError("PayPal 503", "", 503)
    pp.capture_authorization = down
    r = c.post(f"/api/agents/{aid}/reconcile", headers=A).json()
    pp.capture_authorization = real
    assert r["frozen"] and r["unverified"] >= 1 and any("PayPal unavailable" in m for m in r["mismatches"])
    assert not [e for e in led.entries(aid) if e["kind"] == "fund_reversal"]   # no reversal on a transient error
    assert pp._mock_auths[fe["capture_ref"]]["status"] == "CREATED"


def test_declined_capture_is_not_settled_again_by_reconcile(tmp_path):
    c = _client(tmp_path)
    ag = shop_agent(c, daily_cap_usd=500.0, vendor_budgets={"samplebox": 500.0})
    aid = ag["agent_id"]
    c.post(f"/api/agents/{aid}/saved-card", headers=A)
    c.put(f"/api/agents/{aid}/auto-topup", json={"amount_usd": 100, "monthly_cap_usd": 300}, headers=A)
    pp, led = c.app.state.paypal, c.app.state.ledger
    pp.mock_fail["capture_authorization"] = "TRANSACTION_REFUSED"
    assert buy(c, ag["api_key"], **MUG).json()["auto_topup"]["status"] == "capture_declined"
    fe = [e for e in led.entries(aid) if e["kind"] == "fund"][0]
    _age(c, fe["id"], 601)
    c.post(f"/api/agents/{aid}/reconcile", headers=A)
    assert len([e for e in led.entries(aid) if e["kind"] == "fund_reversal"]) == 1 and led.balance(aid) == Decimal("-18.00")


# ---------- (2) schedule after a declined capture / voided hold ----------
def _events_for(c, key, pid):
    return [e for e in c.get("/api/me/schedule", headers={"X-Agent-Key": key}).json()["events"] if e.get("purchaseId") == pid]


def test_schedule_shows_charge_declined_not_needs_approval(tmp_path):
    c = _client(tmp_path)
    ag = shop_agent(c)
    pid = buy(c, ag["api_key"], **SEAT).json()["purchase_id"]
    assert [e["type"] for e in _events_for(c, ag["api_key"], pid)] == ["approval"]
    r = c.post(f"/api/purchases/{pid}/approve-sandbox-card", headers=A | {"X-Simulate-Decline": "TRANSACTION_REFUSED"})
    assert r.json()["status"] == "capture_declined"
    ev = _events_for(c, ag["api_key"], pid)
    assert [e["type"] for e in ev] == ["declined"]
    assert ev[0]["name"].startswith("Charge declined:") and "Needs approval" not in ev[0]["name"]
    assert ev[0]["eventColor"] == "red" and ev[0]["status"] == "capture_declined"


def test_schedule_shows_hold_cancelled_for_voided_approval(tmp_path, monkeypatch):
    from app import procurement as pr
    c = _client(tmp_path)
    ag = shop_agent(c)
    pid = buy(c, ag["api_key"], **SEAT).json()["purchase_id"]
    monkeypatch.setitem(pr.VENDORS["shopsuite"], "fulfillment", "live")       # vendor fails -> hold voided
    monkeypatch.setitem(__import__("app.main", fromlist=["TOOLS"]).TOOLS, "inventory-seat",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("vendor down")))
    assert c.post(f"/api/purchases/{pid}/approve-sandbox-card", headers=A).json()["status"] == "failed_voided"
    ev = _events_for(c, ag["api_key"], pid)
    assert [e["type"] for e in ev] == ["voided"] and ev[0]["name"].startswith("Hold cancelled:")


def test_parallel_reconciles_settle_a_stale_topup_exactly_once(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    for fail in ("", "AUTHORIZATION_EXPIRED"):
        d = tmp_path / (fail or "ok"); d.mkdir()
        c, aid, fe = _crashed_auto_topup(d)
        pp, led = c.app.state.paypal, c.app.state.ledger
        _age(c, fe["id"], 601)
        calls = []
        real = pp.capture_authorization

        def counted(*a, **k):
            calls.append(a[0])
            if fail:
                raise PayPalError("refused", fail, 422)
            return real(*a, **k)
        pp.capture_authorization = counted
        with ThreadPoolExecutor(8) as ex:
            codes = list(ex.map(lambda _: c.post(f"/api/agents/{aid}/reconcile", headers=A).status_code, range(8)))
        assert set(codes) <= {200, 429} and 200 in codes
        assert len(calls) == 1                                      # one capture attempt, however many reconciles
        assert len([e for e in led.entries(aid) if e["kind"] == "fund_reversal"]) == (1 if fail else 0)
        assert led.agent(aid)["frozen_reason"]


def test_agent_key_cannot_reconcile(tmp_path):
    c, aid, fe = _crashed_auto_topup(tmp_path)
    key = shop_agent(c)["api_key"]
    for h in ({"X-Agent-Key": key}, {"Authorization": f"Bearer {key}"}, {"X-Admin-Token": key}):
        assert c.post(f"/api/agents/{aid}/reconcile", headers=h).status_code in (401, 403)
