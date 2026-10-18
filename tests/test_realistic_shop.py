"""feature/realistic-shop: realistic sandbox prices, consistent limits, SiteScope Data rename, no "by WebIntel Tools"."""
import json
import re
from decimal import Decimal
from pathlib import Path

from app import procurement as pr
from app.config import Settings
from tests.test_agenttab import A, _demo_client, fund, shop_agent

ROOT = Path(__file__).parent.parent
D = Decimal


def _price(vendor, item, q=1):
    return pr.price_of(pr.Purchase(vendor, item, q))


def _cfg():
    return pr.AgentConfig(pr.SHOP_POLICY, dict(pr.SHOP_VENDOR_BUDGETS), pr.SHOP_APPROVAL_THRESHOLD,
                          pr.SHOP_PER_PURCHASE_MAX, pr.SHOP_DAILY_CAP)


# ---------- (a) realistic prices and limits ----------
def test_catalog_has_realistic_sandbox_prices():
    assert _price("samplebox", "ceramic-mug-sample") == D("18.00")
    assert _price("samplebox", "linen-napkin-samples") == D("24.00")
    assert _price("samplebox", "throw-blanket-sample") == D("32.00")
    assert _price("shopsuite", "inventory-seat") == D("39.00") and _price("shopsuite", "email-tool-seat") == D("29.00")
    for item in ("page_markdown", "tech_detect", "seo_audit"):                 # research data: $12 to $49
        assert D("12") <= _price("sitescope-data", item) <= D("49")
    assert {_price("sitescope-data", i) for i in ("page_markdown", "tech_detect", "seo_audit")} == {D("12"), D("24"), D("49")}


def test_forbidden_items_are_priced_realistically_and_gift_cards_cost_face_value():
    assert _price("giftcards-now", "giftcard-25") == D("25.00") and _price("giftcards-now", "giftcard-10") == D("10.00")
    for vid, v in pr.VENDORS.items():
        for iid, it in v["items"].items():
            face = re.match(r"\$(\d+) .*gift card", it["name"])
            if face:
                assert it["price"] == D(face.group(1)), iid
            assert it["price"] >= D("10") and it["price"] == it["price"].quantize(D("0.01")), iid   # whole cents


def test_shop_limits_are_consistent():
    assert pr.SHOP_VENDOR_BUDGETS == {"sitescope-data": D("150"), "samplebox": D("200"), "shopsuite": D("300")}
    assert (pr.SHOP_APPROVAL_THRESHOLD, pr.SHOP_PER_PURCHASE_MAX, pr.SHOP_DAILY_CAP) == (D("60"), D("120"), D("250"))
    every = [it["price"] for vid in pr.SHOP_VENDOR_BUDGETS for it in pr.VENDORS[vid]["items"].values()]
    # every allowed item fits a single purchase and its vendor budget; a single item never needs approval on its own,
    # so the approval path is a real multi-unit order (e.g. 2 seats = $78)
    assert max(every) <= pr.SHOP_APPROVAL_THRESHOLD < pr.SHOP_PER_PURCHASE_MAX <= pr.SHOP_DAILY_CAP
    assert all(pr.SHOP_PER_PURCHASE_MAX <= b for vid, b in pr.SHOP_VENDOR_BUDGETS.items() if vid != "sitescope-data")
    assert pr.SHOP_DEMO_FUNDING == D("100.00") and pr.SHOP_DEMO_FUNDING < pr.SHOP_DAILY_CAP


def test_two_seats_need_approval_one_seat_does_not():
    one = pr.decide(_cfg(), pr.Purchase("shopsuite", "inventory-seat", 1, "Inventory software to track our stock levels"),
                    pr.State(), "keyword", None)
    two = pr.decide(_cfg(), pr.Purchase("shopsuite", "inventory-seat", 2, "Inventory software to track our stock levels"),
                    pr.State(), "keyword", None)
    assert (one.outcome, two.outcome, two.amount) == ("allow", "approval", D("78.00"))


def test_every_eval_caps_case_still_trips_its_own_limit():
    cases = {c["id"]: c for c in json.loads((ROOT / "eval/cases.json").read_text(encoding="utf-8"))}
    want = {"caps-01": "per-purchase max", "caps-06": "per-purchase max", "caps-04": "daily cap",
            "caps-02": "30-day budget", "caps-03": "30-day budget", "caps-05": "30-day budget"}
    assert {i for i, c in cases.items() if c["category"] == "caps"} == set(want)
    from eval.run_eval import case_state
    for cid, limit in want.items():
        c = cases[cid]
        p = pr.Purchase(c["vendor"], c["item"], c.get("quantity", 1), c["purpose"], c.get("target", ""))
        out = pr.decide(_cfg(), p, case_state(c), "keyword", None)
        assert (out.outcome, out.layer) == ("block", "caps") and limit in out.reason, (cid, out.reason)
        free = pr.decide(_cfg(), p, pr.State(), "keyword", None)           # without the prior spend it's not a caps block
        assert free.layer != "caps" or "per-purchase" in limit, cid
    for cid in ("legit-15", "legit-16", "legit-17", "legit-18"):            # approval cases really cross the threshold
        c = cases[cid]
        assert pr.SHOP_APPROVAL_THRESHOLD < _price(c["vendor"], c["item"], c["quantity"]) <= pr.SHOP_PER_PURCHASE_MAX, cid


def test_demo_session_is_funded_with_100_and_uses_the_shop_limits(tmp_path):
    j = _demo_client(tmp_path).post("/api/demo/session").json()
    assert j["balance_usd"] == 100.0 and j["approval_threshold_usd"] == 60.0
    assert j["per_call_cap_usd"] == 120.0 and j["daily_cap_usd"] == 250.0
    assert {v["vendor"]: v["budget_usd"] for v in j["vendor_budgets"]} == {"sitescope-data": 150.0, "samplebox": 200.0, "shopsuite": 300.0}


def test_payment_required_hint_asks_for_enough_to_cover_the_item(tmp_path):
    from tests.test_agenttab import buy
    from tests.test_hardening import _client
    c = _client(tmp_path)
    ag = shop_agent(c)
    fund(c, ag["agent_id"], 10)
    r = buy(c, ag["api_key"], vendor="samplebox", item="throw-blanket-sample")    # $32 with a $10 tab
    assert r.status_code == 402
    ask = r.json()["topup"]["body"]["amount_usd"]
    assert ask >= 22 and ask == int(ask)                                         # whole dollars, covers the shortfall


def test_per_ip_llm_budget_is_60_a_day():
    assert Settings().llm_calls_per_ip_day == 60


def test_demo_buttons_show_the_real_catalog_price():
    h = (ROOT / "static/dashboard.html").read_text(encoding="utf-8")
    buttons = re.findall(r"onclick=\"demoBuy\('([^']+)','([^']+)','[^']*','[^']*'(?:,(\d+))?\)\">([^<]*)<span class=\"price\">([^<]*)", h)
    assert len(buttons) == 5
    priced = 0
    for vendor, item, qty, _label, shown in buttons:
        m = re.search(r"\$(\d+(?:\.\d\d)?)", shown)
        if m:
            priced += 1
            assert D(m.group(1)) == _price(vendor, item, int(qty or 1)), (vendor, item, shown)
    assert priced == 3
    assert "funds $100" in h and "$100 of PayPal sandbox money" in h
    assert re.search(r"over <b>\$60</b>", h)


# ---------- (b) branding ----------
def test_no_by_webintel_tools_on_the_page_and_vendor_is_sitescope_data(tmp_path):
    for f in ("static/dashboard.html", "static/app.js", "static/schedule.js", "static/app.css", "app/paypal.py"):
        assert "webintel" not in (ROOT / f).read_text(encoding="utf-8").lower(), f
    assert "webintel-data" not in pr.VENDORS and pr.VENDORS["sitescope-data"]["name"] == "SiteScope Data"
    assert not any("webintel" in json.dumps(c).lower() for c in json.loads((ROOT / "eval/cases.json").read_text(encoding="utf-8")))
    c = _demo_client(tmp_path)
    assert "webintel" not in c.get("/").text.lower()
    assert any(v["vendor"] == "sitescope-data" and v["name"] == "SiteScope Data" for v in c.get("/api/catalog").json()["vendors"])


# ---------- (c) eval re-run: none + keyword only; older modes are flagged, never silently mixed ----------
def test_eval_marks_modes_run_on_older_cases_as_stale(tmp_path):
    from eval import run_eval
    out = tmp_path / "r.json"
    old_ai = {k: v for k, v in json.loads((ROOT / "eval/results.json").read_text(encoding="utf-8"))["modes"]["ai"].items() if k != "cases_sha256"}
    out.write_text(json.dumps({"modes": {"ai": old_ai}}), encoding="utf-8")                     # an ai run without this cases.json's hash
    res = run_eval.main(["--modes", "none,keyword", "--out", str(out)])
    assert res["stale_modes"] == ["ai"]
    assert res["modes"]["keyword"]["cases_sha256"] == run_eval.cases_fingerprint()
    assert "stale_modes" in (ROOT / "static/app.js").read_text(encoding="utf-8")
