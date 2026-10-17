"""Demo buyer agent: plans a research task, buys the data it needs from AgentTab,
reacts to 402 (asks its human for a PayPal top-up) and 403 (guard denial),
then writes a brief.

    python -m agent.buyer_agent --key atk_... --goal "Compare the tech stacks of shopify.com and wix.com" \
        --urls https://www.shopify.com https://www.wix.com
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import httpx

from app import llm as llm_client
from app.config import get_settings

BASE = os.getenv("AGENTTAB_URL", "http://localhost:8000")
SETTINGS = get_settings()  # same LLM config as the server (Gemini when GEMINI_API_KEY is set)


def llm(system: str, user: str) -> str | None:
    if not llm_client.enabled(SETTINGS):
        return None
    try:
        text, _model = llm_client.chat(SETTINGS, [{"role": "system", "content": system},
                                                  {"role": "user", "content": user}], temperature=0.2, timeout=60)
        return text
    except llm_client.LLMError as e:
        print(f"  (LLM unavailable, using fallback: {e})")
        return None


def plan(goal: str, urls: list[str], catalog: dict) -> list[dict]:
    text = llm("You plan purchases for a research agent. Given a goal, candidate URLs and a priced tool catalog, "
               "return a JSON list of steps [{\"tool\":..., \"url\":..., \"purpose\":...}] using the fewest, "
               "cheapest calls that answer the goal. JSON only.",
               json.dumps({"goal": goal, "urls": urls, "catalog": catalog["tools"]}))
    if text:
        try:
            start, end = text.index("["), text.rindex("]") + 1
            steps = json.loads(text[start:end])
            if isinstance(steps, list) and steps:
                return steps
        except ValueError:
            pass
    tool = "tech_detect" if "tech" in goal.lower() or "stack" in goal.lower() else (
        "seo_audit" if "seo" in goal.lower() else "page_markdown")
    return [{"tool": tool, "url": u, "purpose": f"{goal} (data for {u})"} for u in urls]


def run(key: str, goal: str, urls: list[str], log=print) -> dict:
    h = {"X-Agent-Key": key}
    catalog = httpx.get(f"{BASE}/api/catalog", timeout=20).json()
    steps = plan(goal, urls, catalog)
    log(f"Plan: {len(steps)} purchases -> " + ", ".join(f"{s['tool']}({s['url']})" for s in steps))
    results, topup_links = [], []
    for s in steps:
        r = httpx.post(f"{BASE}/api/tools/{s['tool']}", headers=h,
                       json={"url": s["url"], "purpose": s.get("purpose", goal)}, timeout=60)
        if r.status_code == 200:
            j = r.json()
            log(f"  bought {s['tool']} for {s['url']}: -${j['charged_usd']:.2f}, balance ${j['balance_usd']:.2f}")
            results.append({"step": s, "data": j["result"]})
        elif r.status_code == 402:
            t = httpx.post(f"{BASE}/api/me/topup-request", headers=h, json={"amount_usd": 5.0}, timeout=30).json()
            topup_links.append(t.get("approve_url"))
            log(f"  402 payment required; asked my human to approve a $5 PayPal top-up: {t.get('approve_url')}")
            break
        elif r.status_code == 403:
            log(f"  guard denied {s['tool']} on {s['url']}: {r.json()['detail'].get('reason')}")
        else:
            log(f"  {s['tool']} failed ({r.status_code}), not charged")
    brief = None if not results else llm("Write a concise research brief (max 8 bullet points) answering the goal from this purchased data.",
                json.dumps({"goal": goal, "data": results})[:60000])
    if not brief:
        lines = []
        for r in results:
            d = r["data"]
            if "technologies" in d:
                lines.append(f"- {d['url']}: {', '.join(d['technologies']) or 'no known tech detected'}")
            elif "score" in d:
                lines.append(f"- {d['url']}: SEO score {d['score']}; issues: {'; '.join(d['issues']) or 'none'}")
            else:
                lines.append(f"- {d['url']}: {d.get('title','')} ({d.get('chars',0)} chars of markdown)")
        brief = "\n".join(lines) or "No data purchased."
    log("\nBrief:\n" + brief)
    return {"results": results, "brief": brief, "topup_links": topup_links}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--key", required=True)
    p.add_argument("--goal", required=True)
    p.add_argument("--urls", nargs="+", required=True)
    a = p.parse_args()
    out = run(a.key, a.goal, a.urls)
    sys.exit(0 if out["results"] else 1)
