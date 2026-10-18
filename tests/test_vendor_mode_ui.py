"""Vendor-mode dashboard copy (VENDOR_PAYOUTS=1 and a vendor-funded demo agent). Tab mode keeps today's copy.
Static and node checks run without Playwright; the browser check is in tests/test_e2e_vendor_payouts.py."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app import procurement as pr

from test_vendor_payouts_flow import A, BLANKETS, agent, buy, env, ids, make  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent
JS = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
HTML = (ROOT / "static" / "dashboard.html").read_text(encoding="utf-8")
INTRO = ("Start a session: the agent gets a $100 30-day budget, and each purchase is paid by PayPal straight to the "
         "vendor from the owner's saved sandbox card")
TAB_INTRO = ('Start a session: the tab is funded with <b>$100 of PayPal sandbox money</b> (a real sandbox capture with a '
             'test card)')


def _section():
    m = re.search(r"// ---- vendor payouts ----(.*?)// ---- end vendor payouts ----", JS, re.S)
    assert m
    return m.group(1)


def _fn(name):
    m = re.search(rf"function {name}\(\) \{{.*?\n\}}", _section(), re.S)
    assert m, f"no {name}() in the vendor payouts section"
    return m.group(0)


# ---------- tab mode stays exactly as today ----------
def test_tab_mode_copy_is_unchanged_in_the_page():
    assert TAB_INTRO in HTML
    assert re.search(r"1\. Start demo session <span[^>]*class=\"price\"[^>]*>funds \$100</span>", HTML)
    assert re.search(r"<h3[^>]*>Tab and vendor budgets</h3>", HTML)
    assert re.search(r"<button[^>]*onclick=\"demoRefund\(\)\"[^>]*>Refund unused tab credit</button>", HTML)


def test_vendor_mode_is_only_switched_on_by_the_flag_or_a_vendor_agent():
    calls = [JS[max(0, m.start() - 160):m.start()] for m in re.finditer(r"vendorModeUI\(\)", JS)
             if not JS[max(0, m.start() - 9):m.start()].endswith("function ")]
    assert len(calls) == 2, calls
    assert any("'vendors_payable' in h" in c for c in calls)          # /healthz has it only with VENDOR_PAYOUTS=1
    assert any("funding_model === 'vendor'" in c for c in calls)      # /api/me says the demo agent is vendor-funded


# ---------- (1)-(3) vendor-mode copy, run in node against stub elements ----------
NODE = shutil.which("node")


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_vendor_mode_copy_in_node(tmp_path):
    js = tmp_path / "vm.js"
    js.write_text("const els = {}; let VMODE = false; const cols = [];\n"
                  "const dPurch = {setGridOption: (k, v) => cols.push([k, v])}; const purchCols = (o, v) => 'vendor:' + v;\n"
                  "const ph = {textContent: 'Start a session to see the tab.'};\n"
                  "const $ = id => els[id] || (els[id] = {id, textContent: 'tab copy', hidden: false, style: {display: ''},\n"
                  "  querySelector: sel => sel === 'p.sub' ? ph : null});\n"
                  + _fn("vendorModeUI") +
                  "\nvendorModeUI();\nels.placeholder = ph;\nprocess.stdout.write(JSON.stringify(els));\n", encoding="utf-8")
    out = json.loads(subprocess.run([NODE, str(js)], capture_output=True, text=True, encoding="utf-8", check=True).stdout)
    assert out["dintro"]["textContent"] == INTRO
    assert out["dstartprice"]["textContent"] == "$100 budget"
    assert out["dbudgeth"]["textContent"] == "Budget and vendors"
    assert out["drefundbtn"]["style"]["display"] == "none"
    assert out["placeholder"]["textContent"] == "Start a session to see the budget."   # before a session: no "tab"
    assert out["drefundtext"]["innerHTML"] == "you can ask the vendor for a refund within <b>7 days</b>"


def test_vendor_mode_copy_static():
    f = _fn("vendorModeUI")
    for s in (INTRO, "'$100 budget'", "'Budget and vendors'", "$('drefundbtn').style.display = 'none'"):
        assert s in f, s
    for id_ in ("dintro", "dstartprice", "dbudgeth", "drefundbtn"):
        assert f'id="{id_}"' in HTML, id_


def test_vendor_mode_copy_rules():
    f = _fn("vendorModeUI")
    for bad in (r"\bms\b", r"Claude|Gemini|GPT|Anthropic|OpenAI|haiku", r"merchant", r"\btab\b", r"\d+ (calls|cases|tests)"):
        assert not re.search(bad, f, re.I), bad


# ---------- (5) console: the PAID stamp alone for vendor-paid purchases ----------
def test_vendor_paid_console_has_the_stamp_and_no_bought_pill():
    vline = next(l for l in JS.splitlines() if "r.status === 200 && j.funding === 'vendor'" in l)
    assert '<span class="stamp paid">Paid</span>' in vline and "pill('allow', 'Bought')" not in vline
    tline = next(l for l in JS.splitlines() if l.strip().startswith("else if (r.status === 200) {"))
    assert "pill('allow', 'Bought')" in tline                         # tab mode keeps its pill


# ---------- (4) Bryntum agent row ----------
def _agent_row(c, key):
    j = c.get("/api/me/schedule", headers={"X-Agent-Key": key}).json()
    return next(r for r in j["resources"] if r["kind"] == "agent")


def test_schedule_agent_row_shows_the_budget_left_for_vendor_agents(env):
    c, pp, L = env
    a = agent(c, L, envelope=100.0)
    assert _agent_row(c, a["api_key"])["detail"] == "budget $100 left"
    assert buy(c, a).status_code == 200                                # $24 paid to the vendor
    assert _agent_row(c, a["api_key"])["detail"] == "budget $76 left"
    assert buy(c, a, BLANKETS).status_code == 202                      # $64 set aside for approval counts too
    assert _agent_row(c, a["api_key"])["detail"] == "budget $12 left"


def test_schedule_agent_row_shows_cents_when_the_budget_has_them(env):
    c, pp, L = env
    a = agent(c, L, envelope=100.5)
    assert _agent_row(c, a["api_key"])["detail"] == "budget $100.50 left"


def test_schedule_agent_row_keeps_the_tab_for_tab_agents(env):
    c, pp, L = env
    r = c.post("/api/agents", json={"name": "t", "policy": pr.SHOP_POLICY, "daily_cap_usd": 50.0}, headers=A)
    j = r.json()
    assert _agent_row(c, j["api_key"])["detail"] == "tab $0.00"


def test_schedule_agent_row_unchanged_with_the_flag_off(tmp_path, ids):
    c, pp, L = make(tmp_path)
    a = agent(c, L)
    c2, pp2, L2 = make(tmp_path, vendor_payouts=False)
    assert _agent_row(c2, a["api_key"])["detail"] == "tab $0.00"
