"""Video gaps (reviewer t76u, 2026-10-19): the screen and docs/video-script.md must say the same thing.
UI: "×{qty}" after the item when more than one is bought (both table modes); the MCP panel says "the same guard and
limits"; the Bryntum vendor rows show the short vendor name with the full name in a tooltip.
Script: 2b quotes "Stopped by: AI guard (Claude)"; 2c says the row is "held" and quotes the reason with Claude's note
in brackets; section 3 describes the one table plus the held-out line, every voiceover number from eval/results.json and
eval/heldout-results.json. Recording rules: public DNS for example.com, never ALLOW_PRIVATE_TARGETS_FOR_LOCAL_DEV, mouse
outside tables, toasts gone, no filming on a clock-change day. Browser checks are in tests/test_e2e_vendor_payouts.py."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app import procurement as pr

from test_vendor_payouts_flow import BLANKETS, agent, buy, env, ids  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent
JS = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
SCHED = (ROOT / "static" / "schedule.js").read_text(encoding="utf-8")
HTML = (ROOT / "static" / "dashboard.html").read_text(encoding="utf-8")
VIDEO = (ROOT / "docs" / "video-script.md").read_text(encoding="utf-8")
RESULTS = json.loads((ROOT / "eval" / "results.json").read_text(encoding="utf-8"))
HELDOUT = json.loads((ROOT / "eval" / "heldout-results.json").read_text(encoding="utf-8"))
NODE = shutil.which("node")


def _sec(start: str, end: str) -> str:
    return VIDEO[VIDEO.index(start):VIDEO.index(end)]


def _voiceover(text: str) -> str:
    return " ".join(re.findall(r"^> (.*)$", text, re.M))


def _fn(src, name):
    m = re.search(rf"function {name}\(.*?\n(?=\S)", src, re.S)
    assert m, f"no {name}()"
    return m.group(0)


# ---------- (6) Item column: "×{qty}" ----------
def _item_cells(vendor_mode: bool, funding: str, qty) -> str:
    vsec = re.search(r"// ---- vendor payouts ----(.*?)// ---- end vendor payouts ----", JS, re.S).group(1)
    label = re.search(r"^const itemLabel = .*$", JS, re.M)
    src = ("const hhmm = p => 'T'; const vstatus = d => 'S'; const pillCell = p => 'P'; const vactions = (d, o) => 'A';\n"
           "const itemName = (v, i) => 'Inventory app seat'; const vendorName = v => 'ShopSuite';\n"
           "const vendorShort = v => 'ShopSuite';\n" + (label.group(0) + "\n" if label else "") +
           _fn(vsec, "vendorPurchCols") + _fn(JS, "purchCols") +
           f"\nconst cols = purchCols(false, {str(vendor_mode).lower()});\n"
           f"const row = {{data: {{vendor: 'shopsuite', item: 'inventory-seat', funding: '{funding}', status: 'completed', quantity: {json.dumps(qty)}}}}};\n"
           "const c = cols.find(c => c.headerName === 'Item');\n"
           "process.stdout.write(JSON.stringify({v: c.valueGetter(row), t: c.tooltipValueGetter(row)}));\n")
    return json.loads(subprocess.run([NODE, "-e", src], capture_output=True, text=True, encoding="utf-8", check=True).stdout)


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_item_column_shows_the_quantity_in_vendor_mode():
    assert _item_cells(True, "vendor", 2)["v"] == "Inventory app seat ×2"
    assert "Inventory app seat ×2" in _item_cells(True, "vendor", 2)["t"]
    assert _item_cells(True, "vendor", 1)["v"] == "Inventory app seat"           # one item: no "×1"
    assert _item_cells(True, "vendor", None)["v"] == "Inventory app seat"


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_item_column_shows_the_quantity_in_tab_mode():
    assert _item_cells(False, "tab", 2)["v"] == "ShopSuite · Inventory app seat ×2"
    assert _item_cells(False, "vendor", 3)["v"] == "Inventory app seat ×3 · ShopSuite"
    assert _item_cells(False, "tab", 1)["v"] == "ShopSuite · Inventory app seat"


def test_purchase_rows_carry_the_quantity(env):
    c, pp, L = env
    a = agent(c, L)
    r = buy(c, a, BLANKETS)
    assert r.status_code in (200, 202), r.text
    rows = c.get("/api/me/purchases", headers={"X-Agent-Key": a["api_key"]}).json()
    assert [x["quantity"] for x in rows if x["item"] == "throw-blanket-sample"] == [2]


# ---------- (7) MCP panel ----------
def test_mcp_panel_says_the_same_guard_and_limits():
    mcp = HTML[HTML.index('id="mcp"') if 'id="mcp"' in HTML else HTML.index("any MCP client"):]
    assert "the same guard and limits" in mcp[:1500].lower()
    assert "same guard and tab" not in HTML.lower()


# ---------- (10) Bryntum vendor rows ----------
def test_schedule_vendor_rows_carry_the_short_name(env):
    c, pp, L = env
    a = agent(c, L)
    j = c.get("/api/me/schedule", headers={"X-Agent-Key": a["api_key"]}).json()
    vend = [r for r in j["resources"] if r["kind"] == "vendor"]
    assert vend
    for r in vend:
        vid = r["id"].split(":", 1)[1]
        assert r["short"] == pr.VENDOR_SHORT[vid] and r["name"] == pr.VENDORS[vid]["name"]


def test_schedule_shows_the_short_name_with_the_full_name_as_a_tooltip():
    cols = SCHED[SCHED.index("columns: [{text: 'Agent / vendor'"):SCHED.index("features: {")]
    assert "record.kind === 'vendor' ? record.short || record.name : record.name" in cols.replace("(", "").replace(")", "")
    assert re.search(r"tooltipRenderer: \(\{record\}\) => record\.kind === 'vendor' && record\.short", cols)
    assert re.search(r"cellTooltip: \{", SCHED)


# ---------- script (2) 2b ----------
def test_2b_quotes_the_stopped_by_line_from_the_screen():
    b2 = _sec("### 2b.", "### 2c.")
    on_screen = b2[:b2.index("| Claim |")]
    assert '"Stopped by: AI guard (Claude)"' in on_screen
    assert "'policy-ai': 'AI guard (Claude)'" in JS and "'Stopped by: ' + (STOPPED_BY[layer]" in JS
    assert "layer" not in on_screen.lower()                    # the layer name stays only in the backup table


# ---------- script (4)+(5) 2c ----------
CLAUDE_NOTE = ("ShopSuite is not a social network; inventory software seats are allowed business software for running the "
               "shop.")


def test_2c_says_held_and_quotes_the_reason_with_claudes_note():
    c2 = _sec("### 2c.", "### 2d.")
    on_screen = re.sub(r"\s+", " ", c2[:c2.index("| Claim |")])
    assert "`held`" in on_screen and "pending approval" not in on_screen
    assert "pending_approval: 'held'" in JS
    assert f'"$78.00 is above the $60.00 approval threshold; policy check passed ({CLAUDE_NOTE})"' in on_screen
    assert 'f"policy check passed ({d.reason})"' in (ROOT / "app" / "procurement.py").read_text(encoding="utf-8")
    assert "Inventory app seat ×2" in on_screen


# ---------- script (8)+(9) section 3 ----------
_ONES = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split()
_TENS = {2: "twenty", 3: "thirty", 4: "forty", 5: "fifty", 6: "sixty", 7: "seventy", 8: "eighty", 9: "ninety"}


def _w(n: int) -> str:
    if n < 20:
        return _ONES[n]
    t, o = divmod(n, 10)
    return _TENS[t] + ("-" + _ONES[o] if o else "")


def _numbers_in(text: str) -> set[int]:
    words = {_w(n): n for n in range(100)}
    toks = re.findall(r"[a-z]+(?:-[a-z]+)?|\d+", text.lower())
    return {words[t] for t in toks if t in words} | {int(t) for t in toks if t.isdigit()}


def _expected():
    m, ind = RESULTS["modes"], RESULTS["by_category"]["indirect_injection"]
    h, hk = HELDOUT["ai"]["summary"], HELDOUT["keyword"]["summary"]
    cases_h = len(HELDOUT["ai"]["rows"])
    phrases = [f"{_w(RESULTS['cases'])} labeled purchases", f"{_w(RESULTS['attacks'])} of them attacks",
               f"rules alone stop {_w(m['keyword']['attacks_stopped'])}",
               f"all {_w(m['ai']['attacks_stopped'])} are stopped", f"all {_w(m['ai']['legit_correct'])} legitimate buys",
               f"{_w(ind['keyword'])} out of {_w(ind['total'])} with rules, {_w(ind['ai'])} out of {_w(ind['total'])} with claude",
               f"{_w(cases_h)} cases the builder never saw",
               f"claude stopped all {_w(h['attacks_stopped'])} attacks"]
    nums = {RESULTS["cases"], RESULTS["attacks"], m["keyword"]["attacks_stopped"], m["ai"]["attacks_stopped"],
            m["ai"]["legit_correct"], ind["keyword"], ind["ai"], ind["total"], cases_h, h["attacks_stopped"]}
    return phrases, nums, (m, h, hk, cases_h)


def test_section_3_voiceover_numbers_come_from_the_results_files():
    m, h = RESULTS["modes"], HELDOUT["ai"]["summary"]
    assert m["ai"]["attacks_stopped"] == m["ai"]["attacks_total"] and h["attacks_stopped"] == h["attacks_total"]  # "all"
    assert m["ai"]["legit_correct"] == m["ai"]["legit_total"] and h["false_blocks"] == 0                            # "nothing"
    phrases, nums, _ = _expected()
    vo = _voiceover(_sec("## 3. Proof", "## 4. PayPal")).lower()
    for p in phrases:
        assert p in vo, p
    assert "wrongly blocked nothing" in vo
    assert _numbers_in(vo) <= nums, _numbers_in(vo) - nums      # no number that the files don't back
    assert "not a held-out" not in vo


def test_section_3_describes_the_one_table_and_the_held_out_line_on_screen():
    s3 = _sec("## 3. Proof", "## 4. PayPal")
    on_screen = re.sub(r"\s+", " ", s3[:s3.index("> ")])
    assert "one table" in on_screen.lower() and "README" not in on_screen
    for label in ("No guard", "Rules only", "Rules + Claude"):              # the row labels app.js renders
        assert f"['{ {'No guard': 'none', 'Rules only': 'keyword', 'Rules + Claude': 'ai'}[label] }', '{label}']" in JS
        assert f"| {label} |" in s3
    _, _, (m, h, hk, n) = _expected()
    line = (f"A test the builder never saw: Claude stopped {h['attacks_stopped']} / {h['attacks_total']} attacks and allowed "
            f"{h['legit_correct']} / {h['legit_total']} legit buys, {h['false_blocks']} wrongly blocked. Rules alone: "
            f"{hk['attacks_stopped']} / {hk['attacks_total']}. ({n} cases written separately, run once.)")
    assert f'"{line}"' in on_screen
    assert "TBD" not in s3 and "placeholder" not in s3.lower()
    for k, label in (("none", "No guard"), ("keyword", "Rules only"), ("ai", "Rules + Claude")):
        row = next(l for l in s3.splitlines() if l.startswith(f"| {label} |"))
        cells = [re.sub(r"\*", "", x).strip() for x in row.strip("|").split("|")]
        assert cells[1:] == [f"{m[k]['attacks_stopped']} / {m[k]['attacks_total']}",
                             f"{m[k]['legit_correct']} / {m[k]['legit_total']}", str(m[k]["false_blocks"])], row
    assert "tests/test_video_gaps.py::test_section_3_voiceover_numbers_come_from_the_results_files" in s3
    assert "eval/heldout-results.json" in s3


# ---------- recording rules (1), (10)+(12), (11) ----------
def test_recording_rules_check_dns_toasts_mouse_and_clock_change():
    rules = re.sub(r"\s+", " ", _sec("## Recording rules", "## Timeline"))
    assert "socket.gethostbyname('example.com')" in rules and "public address" in rules
    assert "VPN" in rules and "fake-IP" in rules
    assert "Never film with `ALLOW_PRIVATE_TARGETS_FOR_LOCAL_DEV`" in rules
    assert "mouse outside the tables" in rules.lower() and "toast" in rules.lower()
    assert "clock change" in rules.lower() and "DST" in rules


# ---------- t77u (C) rehearsal facts ----------
def test_2b_table_says_claude_blocked_it_in_the_rehearsal():
    b2 = _sec("### 2b.", "### 2c.")
    row = next(l for l in b2.splitlines() if l.startswith("| Claude blocks it |"))
    assert "Claude blocked it in the local sandbox rehearsal on 2026-10-19" in row
    assert "UNSURE" not in row and "has not been run" not in row


def test_recording_rules_name_the_sandbox_rehearsal():
    rules = re.sub(r"\s+", " ", _sec("## Recording rules", "## Timeline"))
    assert "one local sandbox rehearsal of this sequence ran on 2026-10-19" in rules
    assert "no local sandbox run of this exact sequence yet" not in rules
