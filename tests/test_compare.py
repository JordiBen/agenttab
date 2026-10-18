"""feature/compare-panel: POST /api/compare runs ONE purchase through "Rules + keyword check" and "Rules + AI"
side by side. Dry run: no PayPal call, no ledger write, no fetch; its own AI budget; AI trouble means HELD."""
import json
import re
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import paypal as paypal_mod
from app import tools
from app.config import Settings
from app.guard import SYSTEM
from app.main import create_app
from tests.test_claude_guard import NO_NET, OVERLOADED, RATE_LIMIT, _fake, _msg
from tests.test_hardening import _client

CLAUDE = {"guard_provider": "anthropic", "anthropic_api_key": "test-not-a-key"}


def V(reason="fits the policy", allow=True, injection=False):
    return _msg(json.dumps({"target_allowed": allow, "purpose_fits_policy": allow, "injection_attempt": injection,
                            "reason": reason}))


def ip(n):
    return {"CF-Connecting-IP": f"9.9.9.{n}"}


def custom(**kw):
    return {"vendor": "samplebox", "item": "ceramic-mug-sample", "quantity": 1,
            "purpose": "Quality-check a stoneware mug from a supplier before we stock it", "target": ""} | kw


# ---------- presets ----------
def test_presets_are_one_per_attack_category_indirect_injection_first_then_two_legit(tmp_path):
    cases = _client(tmp_path).get("/api/compare/cases").json()["cases"]
    assert cases[0]["id"] == "indirect_injection-05"
    cats = [c["category"] for c in cases]
    assert len(set(cats[:-2])) == len(cats[:-2]) == 8 and cats[-2:] == ["legit", "legit"]


def test_indirect_injection_preset_rules_allow_it_and_claude_blocks_it(tmp_path, monkeypatch):
    fake = _fake(monkeypatch, [V("the purpose relays a web page's instruction", allow=False, injection=True)])
    j = _client(tmp_path, **CLAUDE).post("/api/compare", json={"case_id": "indirect_injection-05"}, headers=ip(1)).json()
    assert j["rules"]["label"] == "Rules + keyword check" and j["ai"]["label"] == "Rules + AI"
    assert j["rules"]["verdict"] == "allowed" and j["rules"]["layer"] == "policy-keyword"
    assert j["ai"]["verdict"] == "blocked" and j["ai"]["llm_called"] is True and len(fake.calls) == 1
    assert j["expected"] == "blocked" and j["dry_run"] is True


def test_rules_side_never_calls_an_ai_and_a_rule_stop_skips_claude(tmp_path, monkeypatch):
    fake = _fake(monkeypatch, [V()])
    j = _client(tmp_path, **CLAUDE).post("/api/compare", json=custom(vendor="giftcards-now", item="giftcard-25"),
                                          headers=ip(1)).json()
    assert j["rules"]["verdict"] == j["ai"]["verdict"] == "blocked" and j["ai"]["llm_called"] is False
    assert fake.calls == []                                   # forbidden vendor: stopped before Claude


# ---------- what the response may carry ----------
def test_response_never_includes_the_system_prompt_even_when_claude_quotes_it(tmp_path, monkeypatch):
    _fake(monkeypatch, [V(SYSTEM[:190], allow=False)])
    r = _client(tmp_path, **CLAUDE).post("/api/compare", json={"case_id": "legit-01"}, headers=ip(1))
    flat = " ".join(r.text.split()).lower()
    sys_flat = " ".join(SYSTEM.split()).lower()
    assert not any(sys_flat[i:i + 50] in flat for i in range(len(sys_flat) - 50))
    assert "withheld" in r.json()["ai"]["reason"]


def test_claude_reason_is_cut_to_200_characters(tmp_path, monkeypatch):
    _fake(monkeypatch, [V("x" * 200)])                       # approval path appends to the reason ($78 > $60)
    j = _client(tmp_path, **CLAUDE).post("/api/compare", json=custom(vendor="shopsuite", item="inventory-seat", quantity=2,
                                          purpose="Two inventory seats so the warehouse team can track stock"), headers=ip(1)).json()
    assert j["ai"]["verdict"] == "held" and j["ai"]["layer"] == "approval"
    assert len(j["ai"]["reason"]) <= 200 and len(j["rules"]["reason"]) <= 200


def test_a_cache_hit_is_flagged_cached_and_costs_no_claude_call(tmp_path, monkeypatch):
    fake = _fake(monkeypatch, [V()])
    c = _client(tmp_path, **CLAUDE)
    a = c.post("/api/compare", json={"case_id": "legit-01"}, headers=ip(1)).json()["ai"]
    b = c.post("/api/compare", json={"case_id": "legit-01"}, headers=ip(1)).json()["ai"]
    assert a["cached"] is False and a["llm_called"] is True
    assert b["cached"] is True and b["llm_called"] is False and b["verdict"] == a["verdict"] == "allowed"
    assert len(fake.calls) == 1


# ---------- dry run: nothing leaves, nothing is written ----------
def _rows(db):
    con = sqlite3.connect(db)
    try:
        names = [r[0] for r in con.execute("select name from sqlite_master where type='table'")]
        return {n: con.execute(f'select count(*) from "{n}"').fetchone()[0] for n in names}
    finally:
        con.close()


def test_zero_paypal_calls_zero_ledger_writes_zero_fetches(tmp_path, monkeypatch):
    _fake(monkeypatch, [V()])
    calls = []
    for name, fn in list(vars(paypal_mod.PayPalClient).items()):
        if callable(fn) and not name.startswith("__"):
            monkeypatch.setattr(paypal_mod.PayPalClient, name,
                                (lambda n, f: lambda *a, **k: (calls.append("paypal." + n), f(*a, **k))[1])(name, fn))
    monkeypatch.setattr(tools, "fetch", lambda *a, **k: calls.append("fetch") or pytest.fail("fetched a URL"))
    import httpx
    real_send = httpx.HTTPTransport.handle_request
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", lambda *a, **k: calls.append("http") or real_send(*a, **k))
    monkeypatch.setattr(tools, "_resolve", lambda host: ["93.184.216.34"])    # check_url: DNS only, never HTTP
    c = _client(tmp_path, **CLAUDE)
    before = _rows(tmp_path / "r.db")
    for body in ({"case_id": "indirect_injection-05"}, {"case_id": "legit-01"},     # legit-02 left the presets (review 6)
                 custom(target="https://www.example.com/catalog", purpose="Mug sample to check glaze quality")):
        j = c.post("/api/compare", json=body, headers=ip(1)).json()
        assert (j["paypal_calls"], j["ledger_writes"], j["fetches"]) == (0, 0, 0)
    assert calls == [] and _rows(tmp_path / "r.db") == before


# ---------- AI trouble: HELD for a human, never ALLOWED ----------
@pytest.mark.parametrize("trouble", ["no_provider", "rate_limit", "overloaded", "no_network", "budget"])
def test_ai_unavailable_or_busy_is_held_for_a_human_never_allowed(tmp_path, monkeypatch, trouble):
    kw = {} if trouble == "no_provider" else dict(CLAUDE)
    if trouble == "budget":
        kw["compare_llm_calls_per_day"] = 0
        _fake(monkeypatch, [V()])
    elif trouble != "no_provider":
        _fake(monkeypatch, [{"rate_limit": RATE_LIMIT, "overloaded": OVERLOADED, "no_network": NO_NET}[trouble]])
    j = _client(tmp_path, **kw).post("/api/compare", json={"case_id": "legit-01"}, headers=ip(1)).json()
    assert j["rules"]["verdict"] == "allowed"                 # the purchase itself is fine...
    assert j["ai"]["verdict"] == "held" and j["ai"]["ai_unavailable"] is True     # ...but no AI verdict = a human decides
    assert j["ai"]["reason"] == "AI unavailable, human approval"


# ---------- its own budget ----------
def test_defaults_5_per_ip_hour_60_runs_and_40_ai_calls_a_day():
    s = Settings()
    assert (s.compare_runs_per_ip_hour, s.compare_runs_per_day, s.compare_llm_calls_per_day) == (5, 60, 40)


def test_compare_ai_budget_is_separate_and_never_starves_the_gauntlet(tmp_path, monkeypatch):
    fake = _fake(monkeypatch, [V()])
    c = _client(tmp_path, compare_llm_calls_per_day=1, gauntlet_llm_calls_per_day=1, **CLAUDE)
    # Custom purchases, not presets: the Gauntlet samples eval cases at random and shares the verdict cache, so a preset
    # used here could come back from the cache as a second policy-ai row with no call (the old ~1-in-10 flake).
    assert c.post("/api/compare", json=custom(purpose="Mug sample to check the glaze before we stock it"), headers=ip(1)).json()["ai"]["llm_called"] is True
    second = c.post("/api/compare", json=custom(purpose="Second mug sample, another glaze"), headers=ip(2)).json()["ai"]
    assert second["verdict"] == "held" and len(fake.calls) == 1        # compare budget (1) used up
    g = c.post("/api/gauntlet", headers=ip(3)).json()
    assert len(fake.calls) == 2 and sum(r["layer"] == "policy-ai" for r in g["cases"]) == 1   # its own 1 call is intact


def test_compare_calls_count_against_the_visitors_per_ip_ai_budget(tmp_path, monkeypatch):
    fake = _fake(monkeypatch, [V()])
    c = _client(tmp_path, llm_calls_per_ip_day=1, **CLAUDE)
    c.post("/api/compare", json={"case_id": "legit-01"}, headers=ip(1))
    held = c.post("/api/compare", json={"case_id": "legit-12"}, headers=ip(1)).json()["ai"]
    assert held["verdict"] == "held" and len(fake.calls) == 1
    other = c.post("/api/compare", json={"case_id": "legit-12"}, headers=ip(2)).json()["ai"]
    assert other["llm_called"] is True and len(fake.calls) == 2


def test_rate_limits_per_ip_per_hour_and_per_day(tmp_path):
    c = _client(tmp_path)
    codes = [c.post("/api/compare", json={"case_id": "legit-01"}, headers=ip(1)).status_code for _ in range(6)]
    assert codes == [200] * 5 + [429]
    assert c.post("/api/compare", json={"case_id": "legit-01"}, headers=ip(2)).status_code == 200
    d = _client(tmp_path, name="d.db", compare_runs_per_day=3, compare_runs_per_ip_hour=100)
    codes = [d.post("/api/compare", json={"case_id": "legit-01"}, headers=ip(n)).status_code for n in range(4)]
    assert codes == [200, 200, 200, 429]


# ---------- input ----------
@pytest.mark.parametrize("body,code", [
    (custom(purpose="a" * 301), 422), (custom(target="https://example.com/" + "a" * 290), 422),
    (custom(vendor="evil-shop"), 422), (custom(item="giftcard-25"), 422), (custom(vendor="", item=""), 422),
    (custom(purpose="mug\x00sample"), 422), (custom(target="javascript:alert(1)"), 422),
    (custom(target="file:///etc/passwd"), 422), ({"case_id": "nope-01"}, 404), ({"case_id": "legit-03"}, 404),
    (custom(purpose="a" * 300), 200),
])
def test_input_is_catalog_only_and_capped_at_300_characters(tmp_path, monkeypatch, body, code):
    monkeypatch.setattr(tools, "_resolve", lambda host: ["93.184.216.34"])
    assert _client(tmp_path).post("/api/compare", json=body, headers=ip(1)).status_code == code


def test_target_must_pass_check_url_private_addresses_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "_resolve", lambda host: ["10.0.0.7"])
    s = Settings(paypal_mode="mock", allow_insecure_mock=True, db_path=str(tmp_path / "p.db"), llm_base_url="",
                 llm_api_key="", admin_token="adm", public_url="http://testserver")
    r = TestClient(create_app(s, allow_private_targets=False)).post(
        "/api/compare", json=custom(target="https://intranet.example/admin"), headers=ip(1))
    assert r.status_code == 422 and r.json()["detail"].startswith("target:")


# ---------- stamps ----------
def test_verdicts_are_only_allowed_blocked_held_never_paid(tmp_path, monkeypatch):
    _fake(monkeypatch, [V()])
    c = _client(tmp_path, compare_runs_per_ip_hour=100, **CLAUDE)
    seen = set()
    for case in c.get("/api/compare/cases").json()["cases"]:
        j = c.post("/api/compare", json={"case_id": case["id"]}, headers=ip(1)).json()
        seen |= {j["rules"]["verdict"], j["ai"]["verdict"]}
        assert "paid" not in json.dumps(j).lower()
    assert seen <= {"allowed", "blocked", "held"} and "allowed" in seen


def test_ui_stamp_words_are_allowed_blocked_held_and_the_left_receipt_says_rules_keyword_check():
    js = open("static/app.js", encoding="utf-8").read()
    words = re.search(r"const CMP_STAMP = \{(.*?)\};", js).group(1)
    assert set(re.findall(r"\['([A-Z]+)'", words)) == {"ALLOWED", "BLOCKED", "HELD"}
    html = open("static/dashboard.html", encoding="utf-8").read()
    # review 6: the column headings match the proof table
    assert 'id="cmpl" aria-label="Rules only"><h4>Rules only</h4>' in html
    assert 'id="cmpr" aria-label="Rules + Claude"><h4>Rules + Claude</h4>' in html


# ---------- review 6 ----------
def test_presets_carry_human_labels_mapped_to_matching_cases(tmp_path):
    cases = {c["id"]: c for c in _client(tmp_path).get("/api/compare/cases").json()["cases"]}
    want = {"indirect_injection-05": ("Instruction hidden in a web page", "indirect_injection"),
            "lookalike_unsafe-01": ("Fake Facebook link", "lookalike_unsafe"),
            "personal_use-01": ("Personal gift disguised as business", "personal_use"),
            "forbidden_vendor-04": ("Gift card", "forbidden_vendor"), "social_target-01": ("Social network research", "social_target"),
            "caps-01": ("Over the budget", "caps"), "vague-01": ("Vague purpose", "vague"),
            "direct_injection-01": ("Order to ignore the rules", "direct_injection"),
            "legit-12": ("Supplier mug sample", "legit"), "legit-01": ("Competitor tech check", "legit")}
    assert {i: (c["label"], c["category"]) for i, c in cases.items()} == want
    assert "faceb00k" in cases["lookalike_unsafe-01"]["target"] and cases["forbidden_vendor-04"]["vendor"] == "giftcards-now"
    assert cases["legit-12"]["item"] == "ceramic-mug-sample" and cases["legit-01"]["item"] == "tech_detect"


def test_compare_returns_the_short_item_name_and_the_price(tmp_path):
    j = _client(tmp_path).post("/api/compare", json={"case_id": "lookalike_unsafe-01"}, headers=ip(1)).json()["purchase"]
    assert (j["item_short"], j["amount_usd"], j["vendor_name"]) == ("Web page copy", 12.0, "SiteScope Data")
    assert j["item_name"].startswith("Clean, LLM-ready Markdown")              # the API keeps the full description


def test_every_catalog_item_has_a_short_name_and_the_page_uses_it(tmp_path):
    from app import procurement as pr
    cat = _client(tmp_path).get("/api/catalog").json()["vendors"]
    shorts = {(v["vendor"], i["item"]): i["short"] for v in cat for i in v["items"]}
    assert all(s and len(s) <= 26 for s in shorts.values()) and len(shorts) == sum(len(v["items"]) for v in pr.VENDORS.values())
    assert shorts[("sitescope-data", "page_markdown")] == "Web page copy"
    assert shorts[("sitescope-data", "tech_detect")] == "Tech stack check"
    assert shorts[("sitescope-data", "seo_audit")] == "SEO audit"
    assert pr.VENDORS["sitescope-data"]["items"]["page_markdown"]["name"].startswith("Clean, LLM-ready")   # prompt input unchanged
    js = open("static/app.js", encoding="utf-8").read()
    assert "return i ? (i.short || i.name) : item;" in js                 # receipts, feed, Gauntlet, showcase
    assert "${enc(i.short || i.name)} ($${i.price_usd})" in js and "`${i.short || i.name} ($${i.price_usd})`" in js


def test_schedule_board_uses_short_item_names():
    from app import schedule
    assert schedule._item_name("sitescope-data", "seo_audit") == "SEO audit"
    assert schedule._item_name("nope", "x") == "x"
