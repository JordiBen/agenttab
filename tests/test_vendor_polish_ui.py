"""Vendor mode polish (VENDOR_PAYOUTS=1, vendor-funded agent): Purchases table with short item names and a narrow Vendor
column, a 'Refund requested' pill with the vendor in its tooltip, sparklines that count like the bars, a HELD verdict
without the duplicate label, and the vendor refund wording. Tab mode keeps today's table and copy. Static, node and
server checks run without Playwright; the browser checks are in tests/test_e2e_vendor_payouts.py."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app import procurement as pr

from test_vendor_payouts_flow import A, BLANKETS, agent, buy, env, ids  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent
JS = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
HTML = (ROOT / "static" / "dashboard.html").read_text(encoding="utf-8")
NODE = shutil.which("node")
TAB_REFUND = "samples and SaaS can be refunded for <b>7 days</b>"


def _section():
    return re.search(r"// ---- vendor payouts ----(.*?)// ---- end vendor payouts ----", JS, re.S).group(1)


def _fn(src, name):
    m = re.search(rf"function {name}\(.*?\n(?=\S)", src, re.S)
    assert m, f"no {name}()"
    return m.group(0)


# ---------- (b) Purchases table ----------
def test_catalog_has_short_vendor_names(env):
    c, pp, L = env
    v = {x["vendor"]: x for x in c.get("/api/catalog").json()["vendors"]}
    assert v["samplebox"]["short"] == "SampleBox" and v["shopsuite"]["short"] == "ShopSuite"
    assert v["sitescope-data"]["short"] == "SiteScope"


def _cols(vendor: bool):
    src = ("const hhmm = p => 'T'; const vstatus = d => 'S'; const pillCell = p => 'P'; const vactions = (d, o) => 'A';\n"
           "const itemName = (v, i) => 'Mug sample'; const vendorName = v => 'SampleBox Wholesale';\n"
           "const vendorShort = v => 'SampleBox';\n" + re.search(r"^const itemLabel = .*$", JS, re.M).group(0) + "\n" +
           _fn(_section(), "vendorPurchCols") + _fn(JS, "purchCols") +
           f"\nconst cols = purchCols(false, {str(vendor).lower()});\n"
           "const row = {data: {vendor: 'samplebox', item: 'ceramic-mug-sample', funding: 'vendor', status: 'completed'}};\n"
           "process.stdout.write(JSON.stringify(cols.map(c => ({h: c.headerName || c.field, id: c.colId, min: c.minWidth || 90,\n"
           "  v: c.valueGetter ? c.valueGetter(row) : null, t: c.tooltipValueGetter ? c.tooltipValueGetter(row) : null}))));\n")
    return json.loads(subprocess.run([NODE, "-e", src], capture_output=True, text=True, encoding="utf-8", check=True).stdout)


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_vendor_table_has_a_short_item_and_a_vendor_column_and_fits_1280():
    cols = _cols(True)
    by = {c["h"]: c for c in cols}
    assert by["Item"]["v"] == "Mug sample" and "SampleBox Wholesale" in by["Item"]["t"]
    assert by["Vendor"]["v"] == "SampleBox" and by["Vendor"]["id"] == "vendor" and by["Vendor"]["min"] <= 100
    assert sum(c["min"] for c in cols) <= 1100            # the Purchases grid is ~1128 px wide at a 1280 px window


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_tab_table_is_unchanged():
    hs = [c["h"] for c in _cols(False)]
    assert hs == ["Time", "Item", "Amount", "status", "Paid from", "Refundable until", "Decided by", "reason", "Actions"]
    assert _cols(False)[1]["v"] == "Mug sample · SampleBox Wholesale"      # vendor rows keep today's tab-mode text


def test_vendor_columns_are_switched_on_only_in_vendor_mode():
    assert "dPurch.setGridOption('columnDefs', purchCols(false, true))" in _fn(_section(), "vendorModeUI")
    assert "oPurch.setGridOption('columnDefs', purchCols(true, vendorAgent))" in _fn(JS, "load")


def test_refund_pill_says_refund_requested_with_the_vendor_in_its_tooltip():
    vs = _fn(_section(), "vstatus")
    assert "pill('refund_requested', 'Refund requested', d.refund.note)" in vs


# ---------- (c) sparkline counts like the bar ----------
def test_vendor_bar_series_counts_like_the_bar(env):
    c, pp, L = env
    a = agent(c, L, envelope=200.0)
    pid = buy(c, a).json()["purchase_id"]                           # $24 paid
    assert buy(c, a, BLANKETS).status_code == 202                   # $64 set aside
    assert c.post(f"/api/purchases/{pid}/refund", headers=A).json()["status"] == "refund_requested"
    b = next(v for v in c.get("/api/me", headers={"X-Agent-Key": a["api_key"]}).json()["vendor_budgets"]
             if v["vendor"] == "samplebox")
    s = b["series_usd"]
    assert len(s) == 30 and s[-1] == b["spent_30d_usd"] == 88.0 and s == sorted(s)


def test_tab_bars_have_no_series(env):
    c, pp, L = env
    r = c.post("/api/agents", json={"name": "t", "policy": pr.SHOP_POLICY, "daily_cap_usd": 50.0,
                                    "vendor_budgets": {"samplebox": 200.0}}, headers=A).json()
    b = c.get("/api/me", headers={"X-Agent-Key": r["api_key"]}).json()["vendor_budgets"][0]
    assert "series_usd" not in b


def test_bars_draw_the_server_series_when_there_is_one():
    assert "spark(v.series_usd || vendorSeries(purchases, v.vendor)" in _fn(JS, "bars")


# ---------- (d) HELD verdict ----------
def test_held_verdict_has_no_duplicate_label_in_vendor_mode():
    line = next(l for l in JS.splitlines() if l.strip().startswith("else if (r.status === 202) {"))
    assert "(VMODE ? '' : pill('approval', 'Needs your approval'))" in line       # tab mode keeps its pill


# ---------- (e) refund wording ----------
def test_tab_refund_wording_is_unchanged_in_the_page():
    assert f'<span id="drefundtext">{TAB_REFUND}</span>' in HTML


def test_vendor_refund_wording():
    assert "$('drefundtext').innerHTML = 'you can ask the vendor for a refund within <b>7 days</b>'" in _fn(_section(), "vendorModeUI")
