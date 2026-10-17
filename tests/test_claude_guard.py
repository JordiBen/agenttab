"""feature/claude-guard: the spend guard on Claude (official anthropic SDK, structured outputs), fail-closed handling,
provider default, production without a provider, real AI status for the badge, eval archive."""
import json
import re
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx
import pytest
from fastapi.testclient import TestClient

from app import llm
from app import procurement as pr
from app.config import Settings
from app.guard import SYSTEM, SpendGuard, VerdictCache
from app.main import AI_BUSY_MESSAGE, create_app
from tests.test_agenttab import A, buy, shop_agent

ROOT = Path(__file__).parent.parent
POLICY = pr.SHOP_POLICY
TOOL = "vendor=SampleBox Wholesale (category: supplier product samples); item=Stoneware mug sample x1"
PURPOSE = "Quality-check a stoneware mug from a supplier before we stock it"
OK = '{"target_allowed": true, "purpose_fits_policy": true, "injection_attempt": false, "reason": "supplier sample fits"}'
S = Settings(guard_provider="anthropic", anthropic_api_key="test-not-a-key", llm_base_url="", llm_api_key="")


def _msg(text=OK, stop="end_turn", thinking_first=True):
    blocks = ([SimpleNamespace(type="thinking", thinking="weighing the policy")] if thinking_first else [])
    blocks.append(SimpleNamespace(type="text", text=text))
    return SimpleNamespace(stop_reason=stop, content=blocks, usage=SimpleNamespace(input_tokens=300, output_tokens=40))


class FakeClaude:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        r = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(r, Exception):
            raise r
        return r


def _fake(monkeypatch, replies):
    fake = FakeClaude(replies)
    monkeypatch.setattr(llm, "_anthropic_client", lambda s: fake)
    return fake


def _req(status):
    return httpx.Response(status, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))


RATE_LIMIT = anthropic.RateLimitError("rate limited", response=_req(429), body=None)
OVERLOADED = anthropic.APIStatusError("overloaded", response=_req(529), body=None)
NO_NET = anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))


def _eval(g, purpose=PURPOSE):
    return g.evaluate(POLICY, TOOL, "", purpose, Decimal("18.00"), skip_prefilter=True)


# ---------- (a) exact SDK call ----------
def test_guard_calls_claude_haiku_with_structured_output_and_nothing_else(monkeypatch):
    fake = _fake(monkeypatch, [_msg()])
    d = _eval(SpendGuard(S))
    assert d.allow and d.source == "ai" and d.model == "claude-haiku-5-5" and d.complete
    (kw,) = fake.calls
    assert set(kw) == {"model", "max_tokens", "system", "messages", "output_config"}     # no temperature, no thinking
    assert kw["model"] == "claude-haiku-5-5" and kw["max_tokens"] == 2000 and kw["system"] == SYSTEM
    assert kw["messages"] == [{"role": "user", "content": SpendGuard._user(POLICY, TOOL, "", PURPOSE, Decimal("18.00"))}]
    oc = kw["output_config"]
    assert oc["effort"] == "low" and oc["format"]["type"] == "json_schema"
    sch = oc["format"]["schema"]
    assert sch["type"] == "object" and sch["additionalProperties"] is False
    assert set(sch["required"]) == {"target_allowed", "purpose_fits_policy", "injection_attempt"}
    assert {k: v["type"] for k, v in sch["properties"].items()} == {
        "target_allowed": "boolean", "purpose_fits_policy": "boolean", "injection_attempt": "boolean", "reason": "string"}


def test_first_text_block_is_parsed_and_injection_flag_blocks(monkeypatch):
    _fake(monkeypatch, [_msg('{"target_allowed": true, "purpose_fits_policy": true, "injection_attempt": true, "reason": "told to approve"}')])
    d = _eval(SpendGuard(S))
    assert not d.allow and "prompt-injection" in d.reason and d.complete


@pytest.mark.parametrize("stop", ["max_tokens", "refusal", "pause_turn", None])
def test_any_stop_reason_but_end_turn_fails_closed_and_is_not_cached(monkeypatch, stop):
    fake = _fake(monkeypatch, [_msg(stop=stop)])
    cache = VerdictCache()
    d = _eval(SpendGuard(S, cache=cache))
    assert not d.allow and d.complete is False and f"stop_reason={stop}" in d.reason
    assert len(cache) == 0 and len(fake.calls) == 1


def test_unparseable_or_missing_text_fails_closed(monkeypatch):
    _fake(monkeypatch, [_msg("sure, approved!")])
    d = _eval(SpendGuard(S))
    assert not d.allow and d.complete is False
    _fake(monkeypatch, [SimpleNamespace(stop_reason="end_turn", content=[], usage=None)])
    d = _eval(SpendGuard(S))
    assert not d.allow and d.complete is False and "no text block" in d.reason


@pytest.mark.parametrize("err", [RATE_LIMIT, OVERLOADED, NO_NET], ids=["429", "529", "connection"])
def test_sdk_errors_mean_unavailable_never_allowed(monkeypatch, err):
    _fake(monkeypatch, [err])
    d = _eval(SpendGuard(S))
    assert d.source == "unavailable" and not d.allow


def test_unavailable_claude_sends_the_purchase_to_human_approval(tmp_path, monkeypatch):
    _fake(monkeypatch, [RATE_LIMIT])
    c = _client(tmp_path)
    ag = shop_agent(c)
    c.post(f"/api/agents/{ag['agent_id']}/topup-sandbox-card", json={"amount_usd": 100}, headers=A)
    r = buy(c, ag["api_key"], vendor="samplebox", item="ceramic-mug-sample", purpose=PURPOSE)
    assert r.status_code == 202 and r.json()["guard"]["layer"] == "policy-unavailable"
    assert r.json()["message"] == AI_BUSY_MESSAGE and c.app.state.ledger.balance(ag["agent_id"]) == Decimal("100.00")


def test_model_id_is_in_the_cache_key(monkeypatch):
    fake = _fake(monkeypatch, [_msg()])
    cache = VerdictCache()
    _eval(SpendGuard(S, cache=cache))
    assert _eval(SpendGuard(S, cache=cache)).cached and len(fake.calls) == 1
    from dataclasses import replace
    other = replace(S, anthropic_model="claude-haiku-5-6")
    assert not _eval(SpendGuard(other, cache=cache)).cached and len(fake.calls) == 2
    user = SpendGuard._user(POLICY, TOOL, "", PURPOSE, Decimal("18.00"))
    assert cache.get(VerdictCache.key(SYSTEM, user, "claude-haiku-5-5")) is not None


# ---------- (b) provider default, key handling ----------
def test_anthropic_is_the_default_provider_with_the_exact_model(monkeypatch):
    for v in ("GUARD_PROVIDER", "ANTHROPIC_MODEL"):
        monkeypatch.delenv(v, raising=False)
    s = Settings()
    assert s.guard_provider == "anthropic" and s.anthropic_model == "claude-haiku-5-5"
    assert llm.guard_model(s) == "claude-haiku-5-5"


def test_sdk_is_pinned_and_key_is_render_secret_never_in_git():
    req = (ROOT / "requirements.txt").read_text()
    assert re.search(r"^anthropic==\d+\.\d+\.\d+$", req, re.M)
    y = (ROOT / "render.yaml").read_text()
    assert re.search(r"- key: ANTHROPIC_API_KEY\n\s+sync: false", y)
    for f in [*ROOT.glob("app/*.py"), *ROOT.glob("static/*.js"), ROOT / "render.yaml", ROOT / "README.md"]:
        assert "sk-ant-" not in f.read_text(), f


def test_gemini_stays_an_optional_provider(monkeypatch):
    calls = []
    monkeypatch.setattr(llm, "chat", lambda s, m, **k: calls.append(m) or (OK, "gemini-3.5-flash-lite"))
    g = SpendGuard(Settings(guard_provider="gemini", llm_base_url="http://llm.invalid", llm_api_key="k",
                            llm_model="gemini-3.5-flash-lite"))
    d = _eval(g)
    assert d.allow and d.model == "gemini-3.5-flash-lite" and len(calls) == 1


# ---------- (d) production without an AI provider: every policy verdict goes to a human ----------
def _sandbox_app(tmp_path, **kw):
    s = Settings(paypal_mode="sandbox", paypal_client_id="x", paypal_client_secret="y", db_path=str(tmp_path / "p.db"),
                 admin_token="z" * 32, public_url="http://testserver", llm_base_url="", llm_api_key="",
                 anthropic_api_key="", **kw)
    c = TestClient(create_app(s, allow_private_targets=True))
    c.app.state.paypal.create_order = lambda amount, aid, desc, op_id="", intent="CAPTURE": {
        "id": "ORD-1", "approve_url": "https://www.sandbox.paypal.com/checkoutnow?token=ORD-1", "status": "CREATED"}
    return c


def test_production_without_ai_provider_holds_every_policy_verdict_for_a_human(tmp_path):
    c = _sandbox_app(tmp_path)
    assert c.get("/healthz").json()["ai_status"] == "busy"
    ag = c.post("/api/agents", json={"name": "p", "policy": POLICY, "daily_cap_usd": 250, "per_call_cap_usd": 120,
                                     "vendor_budgets": {"samplebox": 200}, "approval_threshold_usd": 60},
                headers={"X-Admin-Token": "z" * 32}).json()
    c.app.state.ledger.add_entry(ag["agent_id"], "fund", Decimal("100"), paypal_ref="CAP-TEST")
    r = c.post("/api/purchases", json={"vendor": "samplebox", "item": "ceramic-mug-sample", "purpose": PURPOSE},
               headers={"X-Agent-Key": ag["api_key"]})
    assert r.status_code == 202 and r.json()["guard"]["layer"] == "policy-unavailable"   # never the keyword check
    assert "no AI provider configured" in r.json()["guard"]["reason"] and r.json()["ai_unavailable"] is True
    assert c.app.state.ledger.balance(ag["agent_id"]) == Decimal("100")
    blocked = c.post("/api/purchases", json={"vendor": "giftcards-now", "item": "giftcard-25", "purpose": PURPOSE},
                     headers={"X-Agent-Key": ag["api_key"]})
    assert blocked.status_code == 403            # deterministic layers still block before the policy check


def test_local_offline_mode_keeps_the_keyword_check(tmp_path):
    from tests.test_hardening import _client as plain
    c = plain(tmp_path)
    h = c.get("/healthz").json()
    assert h["ai_status"] == "off" and h["models"] == []


# ---------- (c) real AI state for the badge; model name only in Proof ----------
def _client(tmp_path, **kw):
    s = Settings(paypal_mode="mock", allow_insecure_mock=True, db_path=str(tmp_path / "c.db"), admin_token="adm",
                 public_url="http://testserver", llm_base_url="", llm_api_key="", guard_provider="anthropic",
                 anthropic_api_key="test-not-a-key", **kw)
    return TestClient(create_app(s, allow_private_targets=True))


def test_healthz_reports_live_then_busy_after_a_failure_then_live_again(tmp_path, monkeypatch):
    fake = _fake(monkeypatch, [RATE_LIMIT, _msg()])
    c = _client(tmp_path, tool_calls_per_ip_hour=100)
    h = c.get("/healthz").json()
    assert h["ai_status"] == "live" and h["models"] == ["claude-haiku-5-5"] and h["guard_provider"] == "anthropic"
    ag = shop_agent(c)
    c.post(f"/api/agents/{ag['agent_id']}/topup-sandbox-card", json={"amount_usd": 100}, headers=A)
    assert buy(c, ag["api_key"], vendor="samplebox", item="ceramic-mug-sample", purpose=PURPOSE).status_code == 202
    assert c.get("/healthz").json()["ai_status"] == "busy"
    assert buy(c, ag["api_key"], vendor="samplebox", item="ceramic-mug-sample", purpose=PURPOSE + " today").status_code == 200
    assert c.get("/healthz").json()["ai_status"] == "live" and len(fake.calls) == 2


def test_healthz_is_busy_when_the_daily_ai_budget_is_used_up(tmp_path, monkeypatch):
    _fake(monkeypatch, [_msg()])
    c = _client(tmp_path, llm_calls_per_day=1)
    ag = shop_agent(c)
    c.post(f"/api/agents/{ag['agent_id']}/topup-sandbox-card", json={"amount_usd": 100}, headers=A)
    buy(c, ag["api_key"], vendor="samplebox", item="ceramic-mug-sample", purpose=PURPOSE)
    assert c.get("/healthz").json()["ai_status"] == "busy"


def test_badge_says_live_ai_guard_or_ai_busy_and_model_appears_only_in_proof():
    js, html = (ROOT / "static/app.js").read_text(), (ROOT / "static/dashboard.html").read_text()
    assert "'Live AI guard'" in js and "'AI busy, human approval'" in js and "h.ai_status" in js
    badge = js[js.index("function setLive"):js.index("const pm = $('proofmodel')")]
    assert "models" not in badge and "model" not in badge.replace("proofmodel", "")      # no model name in the badge
    proof = html[html.index('<section id="proof"'):]
    assert 'id="proofmodel"' in proof and 'id="proofmodel"' not in html[:html.index('<section id="proof"')]
    assert "Gemini" not in html and "claude-haiku" not in html
    css = (ROOT / "static/app.css").read_text()
    assert ".live.busy" in css and "#fb923c" in css


# ---------- (e) eval: old Gemini results archived, never deleted ----------
def test_old_gemini_results_are_archived_with_their_cases():
    arch = ROOT / "eval/archive"
    res = json.loads((arch / "results-gemini-2026-10-17-previous-catalog.json").read_text())
    cases = json.loads((arch / "cases-2026-10-17-previous-catalog.json").read_text())
    ai = res["modes"]["ai"]
    assert ai["models"] and all(m.startswith("gemini") for m in ai["models"])
    assert {r["id"] for r in ai["rows"]} == {c["id"] for c in cases} and len(cases) == res["cases"]


def test_a_new_ai_run_archives_the_previous_one_first(tmp_path, monkeypatch):
    from eval import run_eval
    monkeypatch.setattr(run_eval, "ARCHIVE", tmp_path / "archive")
    p = run_eval.archive_mode("ai", {"run_at": "2026-10-17T18:54:23+00:00", "models": ["gemini-3.5-flash-lite"], "rows": []})
    assert p.exists() and json.loads(p.read_text())["models"] == ["gemini-3.5-flash-lite"]
    src = (ROOT / "eval/run_eval.py").read_text()
    assert 'archive_mode("ai", modes["ai"])' in src
