"""Review round 4: AI verdict cache, lower Gauntlet caps + per-IP LLM budget, 'AI guard busy' message, MCP prominence.
All LLM calls here go to a monkeypatched fake; nothing reaches Gemini."""
import json
import re
from decimal import Decimal
from pathlib import Path

import pytest

import app.llm as llm
from app.config import Settings
from app.guard import SpendGuard, VerdictCache
from tests.test_agenttab import A, buy, shop_agent
from tests.test_hardening import _client

ROOT = Path(__file__).resolve().parent.parent
POLICY = "Only buy supplier samples and shop software for our home-goods shop."
TOOL = "vendor=SampleBox (category: samples); item=Stoneware mug sample x1"
PURPOSE = "Quality-check a stoneware mug from a supplier before we stock it"
OK = json.dumps({"target_allowed": True, "purpose_fits_policy": True, "injection_attempt": False, "reason": "fits"})
S_AI = Settings(guard_provider="gemini", llm_base_url="http://llm.invalid", llm_api_key="k", llm_model="primary-model",
                llm_fallback_model="fallback-model")


class FakeLLM:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def __call__(self, s, messages, **k):
        self.calls.append(messages[1]["content"])
        r = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(r, Exception):
            raise r
        return r


def _guard(monkeypatch, replies, clock=None, budget=None):
    fake = FakeLLM(replies)
    monkeypatch.setattr(llm, "chat", fake)
    cache = VerdictCache(ttl_s=600, clock=clock) if clock else VerdictCache(ttl_s=600)
    g = SpendGuard(S_AI, ai_allowed=budget, cache=cache)
    return g, fake, cache


def _eval(g, purpose=PURPOSE, **kw):
    a = dict(policy=POLICY, tool=TOOL, target="", purpose=purpose, price=Decimal("0.20"), skip_prefilter=True) | kw
    return g.evaluate(a["policy"], a["tool"], a["target"], a["purpose"], a["price"], skip_prefilter=True)


# ---------- (1) verdict cache ----------
def test_identical_check_is_served_from_cache_without_spending_budget(monkeypatch):
    spent = []
    g, fake, _ = _guard(monkeypatch, [(OK, "primary-model")], budget=lambda: spent.append(1) or True)
    d1, d2 = _eval(g), _eval(g)
    assert d1.allow and d2.allow and not d1.cached and d2.cached and d2.as_dict()["cached"] is True
    assert len(fake.calls) == 1 and len(spent) == 1               # the hit needed no LLM call and no budget


def test_changing_one_character_of_the_purpose_misses_the_cache(monkeypatch):
    g, fake, _ = _guard(monkeypatch, [(OK, "primary-model")])
    _eval(g)
    d = _eval(g, purpose=PURPOSE[:-1] + "T")                       # one character differs
    assert not d.cached and len(fake.calls) == 2
    for change in (dict(policy=POLICY + "!"), dict(tool=TOOL.replace("x1", "x2")), dict(target="https://a.example"),
                   dict(price=Decimal("0.21"))):
        n = len(fake.calls)
        assert not _eval(g, **change).cached and len(fake.calls) == n + 1, change


def test_model_id_is_part_of_the_key():
    a = VerdictCache.key("sys", "user", "primary-model")
    assert a != VerdictCache.key("sys", "user", "other-model") and a != VerdictCache.key("sys", "user ", "primary-model")


def test_unavailable_fallback_results_are_never_cached(monkeypatch):
    g, fake, cache = _guard(monkeypatch, [llm.LLMError("primary-model: HTTP 429; fallback-model: HTTP 429"),
                                          (OK, "primary-model")])
    d1 = _eval(g)
    assert d1.source == "unavailable" and not d1.allow and len(cache) == 0
    d2 = _eval(g)                                                  # asked again, not a cached "unavailable"
    assert d2.source == "ai" and d2.allow and not d2.cached and len(fake.calls) == 2


def test_budget_exhausted_result_is_not_cached(monkeypatch):
    allow = [False, True]
    g, fake, cache = _guard(monkeypatch, [(OK, "primary-model")], budget=lambda: allow.pop(0))
    assert _eval(g).source == "unavailable" and len(cache) == 0 and fake.calls == []
    assert _eval(g).source == "ai" and len(fake.calls) == 1


def test_fallback_model_and_unparseable_verdicts_are_not_cached(monkeypatch):
    g, fake, cache = _guard(monkeypatch, [(OK, "fallback-model"), ("not json at all", "primary-model"),
                                          (OK, "primary-model")])
    assert _eval(g).model == "fallback-model" and len(cache) == 0
    bad = _eval(g)
    assert not bad.allow and "could not parse" in bad.reason and len(cache) == 0
    assert _eval(g).model == "primary-model" and len(cache) == 1 and len(fake.calls) == 3


def test_heuristic_mode_does_not_use_the_cache():
    cache = VerdictCache()
    g = SpendGuard(Settings(llm_base_url="", llm_api_key=""), cache=cache)
    g.evaluate(POLICY, TOOL, "", PURPOSE, Decimal("0.20"))
    assert len(cache) == 0


def test_cache_entries_expire_after_ten_minutes(monkeypatch):
    now = [1000.0]
    g, fake, _ = _guard(monkeypatch, [(OK, "primary-model")], clock=lambda: now[0])
    _eval(g); now[0] += 599
    assert _eval(g).cached and len(fake.calls) == 1
    now[0] += 2                                                    # 601 s after it was stored
    assert not _eval(g).cached and len(fake.calls) == 2
    assert Settings().verdict_cache_ttl_s == 600


def test_repeated_identical_purchase_spends_one_llm_call_end_to_end(tmp_path, monkeypatch):
    fake = FakeLLM([(OK, "gemini-3.5-flash-lite")])
    monkeypatch.setattr(llm, "chat", fake)
    c = _client(tmp_path, llm_api_key="k", llm_base_url="http://llm.invalid", guard_provider="gemini", llm_model="gemini-3.5-flash-lite")
    ag = shop_agent(c, daily_cap_usd=500.0, vendor_budgets={"samplebox": 500.0})
    c.post(f"/api/agents/{ag['agent_id']}/topup-sandbox-card", json={"amount_usd": 200}, headers=A)
    body = dict(vendor="samplebox", item="ceramic-mug-sample", purpose=PURPOSE)
    r1, r2 = buy(c, ag["api_key"], **body).json(), buy(c, ag["api_key"], **body).json()
    assert r1["guard"]["layer"] == "policy-ai" and r2["guard"].get("cached") is True and len(fake.calls) == 1
    assert c.app.state.ledger.balance(ag["agent_id"]) == Decimal("164.00")  # both bought, each charged once


# ---------- (2) Gauntlet caps and the per-IP LLM budget ----------
def test_gauntlet_caps_are_lower_by_default():
    s = Settings()
    assert s.gauntlet_runs_per_ip_hour <= 2 and s.gauntlet_runs_per_day <= 12 and s.gauntlet_llm_calls_per_day <= 60
    assert s.llm_calls_per_ip_day == 60          # reviewer: per-IP budget raised from 30 to 60 a day


def test_per_ip_llm_budget_sends_one_visitor_to_human_approval_but_not_others(tmp_path, monkeypatch):
    fake = FakeLLM([(OK, "gemini-3.5-flash-lite")])
    monkeypatch.setattr(llm, "chat", fake)
    c = _client(tmp_path, llm_api_key="k", llm_base_url="http://llm.invalid", guard_provider="gemini", llm_calls_per_ip_day=2,
                tool_calls_per_ip_hour=100)
    ag = shop_agent(c, daily_cap_usd=500.0, vendor_budgets={"samplebox": 500.0})
    c.post(f"/api/agents/{ag['agent_id']}/topup-sandbox-card", json={"amount_usd": 300}, headers=A)

    def b(ip, n):
        return c.post("/api/purchases", headers={"X-Agent-Key": ag["api_key"], "CF-Connecting-IP": ip},
                      json=dict(vendor="samplebox", item="ceramic-mug-sample", purpose=f"{PURPOSE} (batch {n})"))
    assert [b("1.1.1.1", n).status_code for n in range(2)] == [200, 200]
    r = b("1.1.1.1", 2)                                            # third distinct check from the same IP
    assert r.status_code == 202 and r.json()["guard"]["layer"] == "policy-unavailable"
    assert b("2.2.2.2", 3).status_code == 200                      # another visitor still gets the AI guard
    assert len(fake.calls) == 3
    assert b("1.1.1.1", 0).json()["guard"].get("cached") is True   # a cache hit needs no budget at all


def test_gauntlet_counts_against_the_same_per_ip_llm_budget(tmp_path, monkeypatch):
    fake = FakeLLM([(OK, "gemini-3.5-flash-lite")])
    monkeypatch.setattr(llm, "chat", fake)
    c = _client(tmp_path, llm_api_key="k", llm_base_url="http://llm.invalid", guard_provider="gemini", llm_calls_per_ip_day=1,
                gauntlet_runs_per_ip_hour=5)
    j = c.post("/api/gauntlet", headers={"CF-Connecting-IP": "3.3.3.3"}).json()
    assert len(fake.calls) == 1                                     # only one LLM call allowed for this IP
    held = [r for r in j["cases"] if r["layer"] == "policy-unavailable"]
    assert held and all(r["outcome"] == "approval" for r in held)  # the rest wait for a human, never allowed


# ---------- (3) AI unavailable: clear message, held for a human ----------
def test_ai_unavailable_purchase_says_guard_busy_waiting_for_human(tmp_path, monkeypatch):
    def down(*a, **k):
        raise llm.LLMError("gemini-3.5-flash-lite: HTTP 429; gemini-3.8-flash: HTTP 429")
    monkeypatch.setattr(llm, "chat", down)
    c = _client(tmp_path, llm_api_key="k", llm_base_url="http://llm.invalid", guard_provider="gemini")
    ag = shop_agent(c)
    c.post(f"/api/agents/{ag['agent_id']}/topup-sandbox-card", json={"amount_usd": 100}, headers=A)
    r = buy(c, ag["api_key"], vendor="samplebox", item="ceramic-mug-sample", purpose=PURPOSE)
    j = r.json()
    assert r.status_code == 202 and j["status"] == "approval_required" and j["ai_unavailable"] is True
    assert j["message"] == "AI guard is busy, purchase is waiting for human approval, by design"
    assert c.app.state.ledger.balance(ag["agent_id"]) == Decimal("100.00")  # nothing charged, nothing auto-approved
    js = (ROOT / "static/app.js").read_text()
    assert "j.ai_unavailable" in js and "dbusy" in js and 'id="dbusy"' in (ROOT / "static/dashboard.html").read_text()


def test_normal_approval_has_no_busy_message(tmp_path):
    c = _client(tmp_path)
    ag = shop_agent(c)
    j = buy(c, ag["api_key"], vendor="shopsuite", item="inventory-seat", quantity=2,
            purpose="Inventory software to track stock across our two warehouses").json()
    assert j["status"] == "approval_required" and "ai_unavailable" not in j and "message" not in j


# ---------- (4) MCP prominence ----------
def _html():
    return (ROOT / "static/dashboard.html").read_text()


def test_hero_has_an_mcp_badge_that_claims_only_what_was_verified():
    h = _html()
    hero = h[h.index('<header class="hero"'):h.index("</header>")]
    m = re.search(r'<a class="chip chip-mcp" id="heromcp" href="#mcp">(.*?)</a>', hero)
    assert m and "Works with any MCP client" in m.group(1)
    # Interop was verified with the official MCP Python SDK only, not with Claude or Cursor apps.
    assert not re.search(r"works with (claude|cursor)", h, re.I)


def test_features_bento_has_an_mcp_card_linking_to_the_panel():
    h = _html()
    bento = h[h.index('class="bento'):h.index('<section id="proof"')]
    assert re.search(r'<a class="tile t-mcp[^"]*" id="bentomcp" href="#mcp">', bento)


def test_mcp_panel_sits_directly_after_the_buy_buttons():
    h = _html()
    guided, mcp, sched = h.index('id="dguided"'), h.index('id="mcp"'), h.index('id="dsched"')
    assert guided < mcp < sched
    between = h[guided:mcp]
    assert "6. Try an injected instruction" in between            # the buy buttons come right before
    assert 'id="dverdict"' not in between and 'id="dbudgets"' not in between   # nothing else in between
