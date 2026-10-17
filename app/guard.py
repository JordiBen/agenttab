"""Spend guard: deterministic hard limits first, then an AI policy check.

The owner writes a plain-English policy ("only buy data about our competitors'
public websites; never more than $1 per day on social media sites"). Before
every metered call the guard asks an LLM whether this purchase, with the
agent's stated purpose, fits that policy. Without an LLM configured it falls
back to a transparent keyword heuristic so the system still runs offline.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import urlparse

import httpx

from .config import Settings


@dataclass
class Decision:
    allow: bool
    reason: str
    source: str  # limit | ai | heuristic

    def as_dict(self):
        return {"allow": self.allow, "reason": self.reason, "source": self.source}


SYSTEM = (
    "You are a strict spending guard for an autonomous AI agent. The agent's owner wrote a spending "
    "policy. Decide if ONE proposed purchase complies. Consider the tool, target and the agent's "
    "stated purpose. If the purpose is vague, unrelated to the policy, or the target is something the "
    "policy forbids, deny. Reply with JSON only: {\"allow\": true|false, \"reason\": \"<max 25 words>\"}."
)


class SpendGuard:
    def __init__(self, settings: Settings):
        self.s = settings

    def hard_limits(self, price: Decimal, balance: Decimal, spent_today: Decimal,
                    daily_cap: Decimal, per_call_cap: Decimal) -> Decision | None:
        if price > per_call_cap:
            return Decision(False, f"price ${price} exceeds per-call cap ${per_call_cap}", "limit")
        if spent_today + price > daily_cap:
            return Decision(False, f"daily cap ${daily_cap} would be exceeded (spent ${spent_today})", "limit")
        if price > balance:
            return Decision(False, "insufficient balance", "limit")
        return None

    def evaluate(self, policy: str, tool: str, target: str, purpose: str, price: Decimal) -> Decision:
        if self.s.llm_base_url and self.s.llm_api_key:
            try:
                return self._ai(policy, tool, target, purpose, price)
            except Exception as e:  # fail closed on garbage, but fall back if the LLM is down
                fallback = self._heuristic(policy, tool, target, purpose)
                fallback.reason = f"{fallback.reason} (AI unavailable: {type(e).__name__})"
                return fallback
        return self._heuristic(policy, tool, target, purpose)

    def _ai(self, policy, tool, target, purpose, price) -> Decision:
        user = json.dumps({"policy": policy, "purchase": {"tool": tool, "target": target,
                                                          "price_usd": str(price), "purpose": purpose}})
        r = httpx.post(
            self.s.llm_base_url.rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {self.s.llm_api_key}"},
            json={"model": self.s.llm_model, "temperature": 0,
                  "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]},
            timeout=30,
        )
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"]
        m = re.search(r"\{.*\}", text, re.S)
        j = json.loads(m.group(0)) if m else {}
        if "allow" not in j:
            return Decision(False, "guard could not parse AI verdict; denied by default", "ai")
        return Decision(bool(j["allow"]), str(j.get("reason", ""))[:200], "ai")

    @staticmethod
    def _heuristic(policy, tool, target, purpose) -> Decision:
        p = policy.lower()
        host = (urlparse(target).hostname or target).lower()
        # "never ... <domain or word>" clauses
        for m in re.finditer(r"(?:never|don't|do not|no)\s+(?:buy|spend|use|fetch)?[^.;]*?([a-z0-9.-]+\.[a-z]{2,}|social media)", p):
            word = m.group(1)
            if word == "social media" and any(s in host for s in ("facebook", "instagram", "tiktok", "x.com", "twitter", "linkedin", "reddit")):
                return Decision(False, "policy forbids social media targets", "heuristic")
            if word in host:
                return Decision(False, f"policy forbids {word}", "heuristic")
        if len(purpose.strip()) < 12:
            return Decision(False, "purpose too vague to justify spend", "heuristic")
        return Decision(True, "no policy clause violated (heuristic mode)", "heuristic")
