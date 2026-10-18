"""Vendor mode, round 2: per-vendor budget bars count what the envelope counts (set aside + paid, incl. refund requested),
the toast says Paid, the refund pill is not clipped, and vendor rows show the short item name. Tab mode is unchanged.
Static checks run without Playwright; the browser checks are in tests/test_e2e_vendor_payouts.py."""
from __future__ import annotations

import re
from pathlib import Path

from app import procurement as pr

from test_vendor_payouts_flow import A, BLANKETS, NAPKINS, agent, buy, env, ids  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent
JS = (ROOT / "static" / "app.js").read_text(encoding="utf-8")


def _bar(c, key, vendor="samplebox"):
    me = c.get("/api/me", headers={"X-Agent-Key": key}).json()
    return next(v for v in me["vendor_budgets"] if v["vendor"] == vendor)


def _sched_budget(c, key, vendor="samplebox"):
    j = c.get("/api/me/schedule", headers={"X-Agent-Key": key}).json()
    return next(e["name"] for e in j["events"] if e["type"] == "budget" and e["resourceId"].endswith(":" + vendor))


# ---------- 1. the bar counts what the envelope counts ----------
def test_vendor_bar_includes_pending_approval_and_splits_paid_and_set_aside(env):
    c, pp, L = env
    a = agent(c, L, envelope=200.0)
    assert buy(c, a).status_code == 200                       # $24 paid
    assert buy(c, a, BLANKETS).status_code == 202             # $64 waiting for approval: set aside
    b = _bar(c, a["api_key"])
    assert (b["spent_30d_usd"], b["paid_usd"], b["set_aside_usd"]) == (88.0, 24.0, 64.0)
    assert _sched_budget(c, a["api_key"]) == "30-day budget: $88.00 of $200.00"


def test_vendor_bar_does_not_drop_after_a_refund_request(env):
    c, pp, L = env
    a = agent(c, L, envelope=200.0)
    pid = buy(c, a).json()["purchase_id"]
    assert c.post(f"/api/purchases/{pid}/refund", headers=A).json()["status"] == "refund_requested"
    b = _bar(c, a["api_key"])
    assert (b["spent_30d_usd"], b["paid_usd"], b["set_aside_usd"]) == (24.0, 24.0, 0.0)   # counts until PayPal refunds
    assert _sched_budget(c, a["api_key"]) == "30-day budget: $24.00 of $200.00"


def test_tab_agent_bars_are_unchanged(env):
    c, pp, L = env
    r = c.post("/api/agents", json={"name": "t", "policy": pr.SHOP_POLICY, "daily_cap_usd": 50.0,
                                         "vendor_budgets": {"samplebox": 200.0}}, headers=A).json()
    b = _bar(c, r["api_key"])
    assert set(b) == {"vendor", "name", "budget_usd", "spent_30d_usd"} and b["spent_30d_usd"] == 0.0


def test_bars_show_the_paid_and_set_aside_split():
    m = re.search(r"function bars\(.*?\n(?=let CAT)", JS, re.S)
    assert m
    line = m.group(0)
    assert "v.paid_usd != null" in line and "paid · " in line and "set aside" in line and "v.set_aside_usd" in line


# ---------- 2. toast ----------
def test_vendor_toast_says_paid_and_tab_toast_says_bought():
    vb = re.search(r"function vendorBought\(j, g\) \{\n(.*?)\n", JS).group(1)
    assert "toast('ok', 'Paid · ' + usd(j.paid_usd)" in vb and "Bought" not in vb
    tline = next(l for l in JS.splitlines() if l.strip().startswith("else if (r.status === 200) {"))
    assert "toast('ok', 'Bought · ' + usd(j.charged_usd)" in tline


# ---------- 3. refund pill fits ----------
def test_status_column_is_wide_enough_for_the_vendor_refund_pill():
    col = next(l for l in JS.splitlines() if "{field: 'status', cellRenderer: p => p.data.funding === 'vendor'" in l)
    w = int(re.search(r"minWidth: (\d+)", col).group(1))
    assert w >= 8 * len("Refund requested"), w               # the pill text; the vendor is in its tooltip and own column


# ---------- 4. short item names for vendor rows ----------
def test_vendor_rows_show_the_short_item_name():
    col = next(l for l in JS.splitlines() if "{colId: 'item', headerName: 'Item'" in l)
    # vendor mode: the short item name alone; the vendor short name has its own column (tests/test_e2e_vendor_payouts.py)
    assert "valueGetter: p => itemName(p.data.vendor, p.data.item)," in col
    tab = next(l for l in JS.splitlines() if "{headerName: 'Item', minWidth: 210" in l)
    assert "vendorName(p.data.vendor) + ' · ' + itemName(p.data.vendor, p.data.item)" in tab   # tab rows unchanged