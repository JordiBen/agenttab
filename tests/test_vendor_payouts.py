"""Vendor payouts, capture-only (docs/vendor-payouts-impl-plan.md), step 1: whole cents, migration, the
VENDOR_PAYOUTS flag, vendor merchant ids from env, the `payee` layer, derived Request-Ids, and the atomic
reservation (reserved + paid count against the caps, written before any PayPal call).
No network: nothing here talks to PayPal or an AI provider."""
from __future__ import annotations

import sqlite3
import threading
import time
from decimal import Decimal

import pytest

from app import procurement as pr
from app import vendorpay as vp
from app.ledger import Ledger, cents_str, parse_cents, to_cents

MID = {"samplebox": "SBXMERCHANT01", "shopsuite": "SHOPMERCHANT2", "sitescope-data": "SITEMERCHANT3"}
ENV = {"samplebox": "VENDOR_SAMPLEBOX_MERCHANT_ID", "shopsuite": "VENDOR_SHOPSUITE_MERCHANT_ID",
       "sitescope-data": "VENDOR_SITESCOPE_MERCHANT_ID"}


@pytest.fixture
def ids(monkeypatch):
    for v, name in ENV.items():
        monkeypatch.setenv(name, MID[v])
    return MID


@pytest.fixture
def no_ids(monkeypatch):
    for name in ENV.values():
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def L(tmp_path):
    return Ledger(str(tmp_path / "v.db"))


def vendor_agent(L, envelope=10_000):
    a = L.create_agent("v", "o@example.com", "policy", Decimal("250"), Decimal("120"),
                       vendor_budgets={"samplebox": Decimal("200"), "shopsuite": Decimal("300")})
    ok, why = L.set_funding_model(a["agent_id"], "vendor", envelope)
    assert ok, why
    return a["agent_id"]


LIM = {"per_purchase_max_cents": 12_000, "daily_cap_cents": 25_000, "vendor_budget_cents": 20_000,
       "envelope_cents_30d": 10_000}


def reserve(L, aid, cents, vendor="samplebox", status="reserved", limits=None, payee="SBXMERCHANT01"):
    return L.reserve_vendor_purchase(aid, vendor, "linen-napkin-samples", 1, cents, "", "napkin samples for catalog",
                                     status, payee, dict(LIM, **(limits or {})), "policy-keyword", "ok")


# ---------- whole cents ----------
def test_to_cents_whole_cents_only():
    assert to_cents(Decimal("12.34")) == 1234
    assert to_cents(Decimal("24")) == 2400
    assert to_cents(Decimal("0.10")) == 10
    for bad in (Decimal("0.001"), Decimal("10.005"), Decimal("-1.00"), Decimal("NaN"), Decimal("Infinity")):
        with pytest.raises(ValueError):
            to_cents(bad)
    with pytest.raises(TypeError):
        to_cents(12.34)          # floats never become money


def test_cents_strings_round_trip():
    for c in (0, 1, 9, 10, 99, 100, 101, 1234, 2400, 999_999):
        s = cents_str(c)
        assert parse_cents(s) == c
        assert s.count(".") == 1 and len(s.split(".")[1]) == 2
    assert cents_str(1000) == "10.00" and cents_str(5) == "0.05"
    for bad in ("10.001", "1e3", "-1.00", "", " 10.00", "10.0.0", "ten", "10,00"):
        with pytest.raises(ValueError):
            parse_cents(bad)
    for bad in (-1, 1.5, "100", True):
        with pytest.raises((ValueError, TypeError)):
            cents_str(bad)


# ---------- flag ----------
@pytest.mark.parametrize("val,on", [(None, False), ("", False), ("0", False), ("true", False), ("yes", False),
                                    (" 1", False), ("1", True)])
def test_flag_off_by_default_and_only_exact_1_turns_it_on(monkeypatch, val, on):
    from app.config import Settings
    if val is None:
        monkeypatch.delenv("VENDOR_PAYOUTS", raising=False)
    else:
        monkeypatch.setenv("VENDOR_PAYOUTS", val)
    assert Settings().vendor_payouts is on


# ---------- merchant ids from env ----------
def test_merchant_ids_from_env_only(ids):
    assert {v: vp.merchant_id(v) for v in ENV} == MID
    assert vp.payable_count() == 3


def test_missing_or_malformed_id_means_not_payable(monkeypatch, no_ids):
    assert vp.merchant_id("samplebox") is None
    for bad in ("", "short", "sbxmerchant01", "SBXMERCHANT01 ", "SBX-MERCHANT1", "SBXMERCHANT0123"):
        monkeypatch.setenv("VENDOR_SAMPLEBOX_MERCHANT_ID", bad)
        assert vp.merchant_id("samplebox") is None, bad
    assert vp.payable_count() == 0


def test_forbidden_and_unknown_vendors_never_get_an_id(monkeypatch, ids):
    monkeypatch.setenv("VENDOR_ADBOOST_SOCIAL_MERCHANT_ID", "ADBMERCHANT01")
    monkeypatch.setenv("VENDOR_GIFTCARDS_NOW_MERCHANT_ID", "GIFTMERCHANT1")
    monkeypatch.setenv("VENDOR_ADBOOST_MERCHANT_ID", "ADBMERCHANT01")
    for v in ("adboost-social", "giftcards-now", "nope", ""):
        assert vp.merchant_id(v) is None


# ---------- derived PayPal-Request-Id ----------
def test_request_id_derived_from_pid():
    pid = "pu_0123456789ab"
    assert vp.request_id(pid, "pay") == "at-pu_0123456789ab-pay"
    assert vp.request_id(pid, "pay") == vp.request_id(pid, "pay")
    assert vp.request_id(pid, "pay") != vp.request_id("pu_0123456789ac", "pay")
    assert len(vp.request_id(pid, "pay")) <= 38
    for bad_pid in ("", "pu_", "pu_xyz", "pu_0123456789abc", "ag_0123456789ab", "pu_0123456789AB", None):
        with pytest.raises(ValueError):
            vp.request_id(bad_pid, "pay")
    with pytest.raises(ValueError):
        vp.request_id(pid, "refund")      # only known actions


# ---------- payee layer ----------
class NoGuard:
    calls = 0

    def evaluate(self, *a, **k):
        NoGuard.calls += 1
        raise AssertionError("the guard must not run")


def cfg(**kw):
    return pr.AgentConfig("policy", {"samplebox": Decimal("200"), "shopsuite": Decimal("300")}, Decimal("60"),
                          Decimal("120"), Decimal("250"), **kw)


GOOD = pr.Purchase("samplebox", "linen-napkin-samples", 1, "linen napkin samples for the spring catalog")


def test_payee_layer_blocks_vendor_without_id_before_guard(no_ids):
    NoGuard.calls = 0
    o = pr.decide(cfg(payee_ids={"samplebox": None, "shopsuite": "SHOPMERCHANT2"}), GOOD, pr.State(), "ai", NoGuard())
    assert (o.outcome, o.layer) == ("block", "payee")
    assert "no PayPal account" in o.reason
    assert NoGuard.calls == 0


def test_payee_layer_reason_never_leaks_an_id(ids):
    o = pr.decide(cfg(payee_ids={"shopsuite": "SHOPMERCHANT2"}), GOOD, pr.State(), "keyword", None)
    assert o.layer == "payee" and "MERCHANT" not in o.reason


def test_payee_layer_passes_vendor_with_id(ids):
    o = pr.decide(cfg(payee_ids=dict(MID)), GOOD, pr.State(), "keyword", None)
    assert o.outcome == "allow" and o.layer == "policy-keyword"


def test_tab_agents_have_no_payee_layer(no_ids):
    c = cfg()
    assert c.payee_ids is None and c.envelope_30d is None
    o = pr.decide(c, GOOD, pr.State(), "keyword", None)
    assert o.outcome == "allow"


def test_from_row_vendor_agent_only_with_flag(L, ids):
    aid = vendor_agent(L)
    a = L.agent(aid)
    assert pr.AgentConfig.from_row(a).payee_ids is None                 # flag off: not vendor-funded
    c = pr.AgentConfig.from_row(a, vendor_payouts=True)
    assert c.payee_ids == MID and c.envelope_30d == Decimal("100.00")
    tab = L.create_agent("t", "o@example.com", "p", Decimal("10"), Decimal("5"))
    assert pr.AgentConfig.from_row(L.agent(tab["agent_id"]), vendor_payouts=True).payee_ids is None


def test_envelope_cap_blocks_before_guard(ids):
    NoGuard.calls = 0
    st = pr.State(envelope_used_30d=Decimal("90.00"))
    o = pr.decide(cfg(payee_ids=dict(MID), envelope_30d=Decimal("100.00")), GOOD, st, "ai", NoGuard())
    assert (o.outcome, o.layer) == ("block", "caps") and "envelope" in o.reason
    assert NoGuard.calls == 0


# ---------- migration ----------
OLD_SCHEMA = """
CREATE TABLE agents(id TEXT PRIMARY KEY, name TEXT, api_key TEXT UNIQUE, owner_email TEXT, policy TEXT,
  daily_cap_micro INTEGER, per_call_cap_micro INTEGER, created REAL);
CREATE TABLE purchases(id TEXT PRIMARY KEY, agent_id TEXT, ts REAL, vendor TEXT, item TEXT, quantity INTEGER,
  amount_micro INTEGER, target TEXT, purpose TEXT, status TEXT, funding TEXT, refund_deadline REAL,
  approval_expires REAL, paypal_order TEXT, paypal_capture TEXT, decision_source TEXT, reason TEXT, fulfillment TEXT);
INSERT INTO agents VALUES('ag_old','old','atk_x','o@example.com','p',10000000,5000000,1);
INSERT INTO purchases(id,agent_id,ts,vendor,item,quantity,amount_micro,status,funding)
  VALUES('pu_old000000001','ag_old',1,'samplebox','ceramic-mug-sample',1,18000000,'completed','tab');
"""


def test_migration_keeps_old_rows_on_tab(tmp_path):
    p = str(tmp_path / "old.db")
    c = sqlite3.connect(p)
    c.executescript(OLD_SCHEMA)
    c.commit()
    c.close()
    L = Ledger(p)
    a = L.agent("ag_old")
    assert a["funding_model"] == "tab" and a["envelope_cents_30d"] is None
    pu = L.purchase("pu_old000000001")
    assert pu["amount_micro"] == 18_000_000 and pu["amount_cents"] is None and pu["payee_merchant_id"] is None
    assert pu["funding"] == "tab" and pu["status"] == "completed"
    Ledger(p)   # idempotent


def test_new_agents_default_to_tab(L):
    a = L.create_agent("n", "o@example.com", "p", Decimal("10"), Decimal("5"))
    assert L.agent(a["agent_id"])["funding_model"] == "tab"


# ---------- cutover ----------
def test_cutover_refused_while_tab_holds_money(L):
    a = L.create_agent("c", "o@example.com", "p", Decimal("10"), Decimal("5"))
    L.add_entry(a["agent_id"], "fund", Decimal("5.00"), "topup", paypal_ref="CAP1")
    ok, why = L.set_funding_model(a["agent_id"], "vendor", 10_000)
    assert not ok and "balance" in why
    assert L.agent(a["agent_id"])["funding_model"] == "tab"


@pytest.mark.parametrize("env", [-1, 1.5, "100", None, True])
def test_cutover_needs_whole_cent_envelope(L, env):
    a = L.create_agent("c", "o@example.com", "p", Decimal("10"), Decimal("5"))
    ok, _ = L.set_funding_model(a["agent_id"], "vendor", env)
    assert not ok and L.agent(a["agent_id"])["funding_model"] == "tab"


def test_cutover_unknown_model_refused(L):
    a = L.create_agent("c", "o@example.com", "p", Decimal("10"), Decimal("5"))
    assert not L.set_funding_model(a["agent_id"], "direct", 100)[0]


# ---------- reservation ----------
def test_reserve_writes_reserved_row_and_no_money_entries(L):
    aid = vendor_agent(L)
    pid, why = reserve(L, aid, 2400)
    assert why == "" and pid.startswith("pu_")
    r = L.purchase(pid)
    assert (r["status"], r["funding"], r["amount_cents"], r["payee_merchant_id"]) == ("reserved", "vendor", 2400, "SBXMERCHANT01")
    assert r["amount_micro"] == 24_000_000            # existing listings keep working
    assert r["paypal_order"] == "" and r["paypal_capture"] == ""
    assert L.entries(aid) == []                        # no fund, no charge: AgentTab holds no money
    assert L.vendor_counted_cents(aid, time.time() - 86400) == 2400


def test_reserve_pending_approval_counts_too(L):
    aid = vendor_agent(L)
    pid, _ = reserve(L, aid, 7000, status="pending_approval")
    assert L.purchase(pid)["status"] == "pending_approval"
    assert L.vendor_counted_cents(aid, 0) == 7000


@pytest.mark.parametrize("status", ["completed", "failed", "paid"])
def test_reserve_only_reserved_or_pending(L, status):
    aid = vendor_agent(L)
    with pytest.raises(ValueError):
        reserve(L, aid, 100, status=status)


def test_reserve_refuses_tab_agent(L):
    a = L.create_agent("t", "o@example.com", "p", Decimal("250"), Decimal("120"))
    pid, why = reserve(L, a["agent_id"], 100)
    assert pid is None and "vendor-funded" in why
    assert L.purchases(a["agent_id"]) == []


def test_reserve_refuses_frozen_agent(L):
    aid = vendor_agent(L)
    L.freeze(aid, "payment_failed on pu_x")
    pid, why = reserve(L, aid, 100)
    assert pid is None and "frozen" in why


@pytest.mark.parametrize("payee", [None, "", "bad"])
def test_reserve_refuses_missing_payee(L, payee):
    aid = vendor_agent(L)
    pid, why = reserve(L, aid, 100, payee=payee)
    assert pid is None and "PayPal account" in why
    assert L.purchases(aid) == []


@pytest.mark.parametrize("amt", [0, -100, 12.5, "100", Decimal("1.00"), True])
def test_reserve_refuses_non_cent_amounts(L, amt):
    aid = vendor_agent(L)
    with pytest.raises((ValueError, TypeError)):
        reserve(L, aid, amt)


def test_reserve_per_purchase_max(L):
    aid = vendor_agent(L, envelope=100_000)
    assert reserve(L, aid, 12_001)[0] is None
    assert reserve(L, aid, 12_000)[0] is not None


def test_reserve_daily_cap_counts_reserved_and_paid(L):
    aid = vendor_agent(L, envelope=100_000)
    lim = {"vendor_budget_cents": None, "daily_cap_cents": 5_000}
    assert reserve(L, aid, 3_000, limits=lim)[0]
    pid, why = reserve(L, aid, 2_001, limits=lim)
    assert pid is None and "daily cap" in why
    assert reserve(L, aid, 2_000, limits=lim)[0]


def test_reserve_vendor_budget_30d(L):
    aid = vendor_agent(L, envelope=100_000)
    lim = {"vendor_budget_cents": 5_000, "daily_cap_cents": 100_000}
    assert reserve(L, aid, 4_000, limits=lim)[0]
    pid, why = reserve(L, aid, 1_001, limits=lim)
    assert pid is None and "30-day budget" in why
    assert reserve(L, aid, 1_001, vendor="shopsuite", limits=lim, payee="SHOPMERCHANT2")[0]   # per vendor


def test_reserve_envelope_30d(L):
    aid = vendor_agent(L, envelope=5_000)
    lim = {"vendor_budget_cents": None, "daily_cap_cents": 100_000, "envelope_cents_30d": 5_000}
    assert reserve(L, aid, 4_000, limits=lim)[0]
    pid, why = reserve(L, aid, 1_001, limits=lim)
    assert pid is None and "envelope" in why


def test_reserve_uses_agent_envelope_not_only_caller_limit(L):
    aid = vendor_agent(L, envelope=1_000)
    pid, why = reserve(L, aid, 1_001, limits={"envelope_cents_30d": 999_999})
    assert pid is None and "envelope" in why


COUNTED = ["reserved", "pending_approval", "completing", "payment_pending", "payment_unknown", "flagged",
           "completed", "refund_requested", "partially_refunded"]
NOT_COUNTED = ["failed", "rejected", "expired", "payment_failed", "refunded"]


@pytest.mark.parametrize("status", COUNTED + NOT_COUNTED)
def test_counted_statuses(L, status):
    aid = vendor_agent(L)
    pid, _ = reserve(L, aid, 4_000)
    L.update_purchase(pid, status=status)
    assert L.vendor_counted_cents(aid, 0) == (4_000 if status in COUNTED else 0)
    assert L.vendor_counted_cents(aid, 0, vendor="samplebox") == (4_000 if status in COUNTED else 0)


def test_windows_24h_vs_30d(L):
    aid = vendor_agent(L, envelope=100_000)
    pid, _ = reserve(L, aid, 4_000)
    L.update_purchase(pid, status="completed", ts=time.time() - 2 * 86400)
    assert L.vendor_counted_cents(aid, time.time() - 86400) == 0
    assert L.vendor_counted_cents(aid, time.time() - 30 * 86400) == 4_000
    L.update_purchase(pid, ts=time.time() - 31 * 86400)
    assert L.vendor_counted_cents(aid, time.time() - 30 * 86400) == 0


def test_tab_purchases_do_not_count_for_vendor_caps(L):
    aid = vendor_agent(L)
    L.add_purchase(aid, "samplebox", "x", 1, Decimal("50"), "", "p", "completed", "tab", None, None, "k", "r")
    assert L.vendor_counted_cents(aid, 0) == 0


def test_refused_reservation_leaves_nothing(L):
    aid = vendor_agent(L, envelope=1_000)
    reserve(L, aid, 5_000)
    assert L.purchases(aid) == [] and not L.db.in_transaction


# ---------- atomic transitions ----------
def test_move_purchase_exactly_one_winner(L):
    aid = vendor_agent(L)
    pid, _ = reserve(L, aid, 7_000, status="pending_approval")
    wins, bar = [], threading.Barrier(8)

    def go():
        bar.wait()
        wins.append(L.move_purchase(pid, ("pending_approval",), "completing"))

    ts = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert wins.count(True) == 1 and L.purchase(pid)["status"] == "completing"


def test_move_purchase_sets_fields_and_respects_from(L):
    aid = vendor_agent(L)
    pid, _ = reserve(L, aid, 100)
    assert not L.move_purchase(pid, ("completing",), "completed", paypal_order="O1")
    assert L.move_purchase(pid, ("reserved",), "failed", reason="fulfil failed")
    r = L.purchase(pid)
    assert (r["status"], r["reason"], r["paypal_order"]) == ("failed", "fulfil failed", "")
    with pytest.raises(ValueError):
        L.move_purchase(pid, ("failed",), "completed", **{"status": "x"})
    with pytest.raises(ValueError):
        L.move_purchase(pid, ("failed",), "completed", **{"amount_cents; DROP": 1})


# ---------- concurrency (plan test 5, ledger level) ----------
def _race(ledgers, aid, n, cents, limits):
    res, bar = [], threading.Barrier(n)

    def go(i):
        bar.wait()
        res.append(reserve(ledgers[i % len(ledgers)], aid, cents, limits=limits)[0])

    ts = [threading.Thread(target=go, args=(i,)) for i in range(n)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    return [r for r in res if r]


@pytest.mark.parametrize("cap", ["daily_cap_cents", "vendor_budget_cents", "envelope_cents_30d"])
def test_parallel_reservations_never_exceed_caps(L, cap):
    aid = vendor_agent(L, envelope=1_000_000)
    lim = {"daily_cap_cents": 1_000_000, "vendor_budget_cents": 1_000_000, "envelope_cents_30d": 1_000_000, cap: 10_000}
    won = _race([L], aid, 10, 3_000, lim)
    assert len(won) == 3 and L.vendor_counted_cents(aid, 0) == 9_000


def test_parallel_reservations_across_two_ledgers_one_file(tmp_path):
    p = str(tmp_path / "shared.db")
    L1, L2 = Ledger(p), Ledger(p)
    aid = vendor_agent(L1, envelope=10_000)
    won = _race([L1, L2], aid, 10, 3_000, {"daily_cap_cents": 1_000_000, "vendor_budget_cents": None})
    assert len(won) == 3 and L2.vendor_counted_cents(aid, 0) == 9_000
