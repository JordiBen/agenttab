"""The schedule: what happens when, per agent and per vendor (rendered with Bryntum Scheduler).

The scheduling agent (sweep) runs on every schedule read and after every PayPal webhook:
  * expires approval links that ran out (the pending-approval bar ends, the purchase turns 'expired');
  * proposes refunds before refund windows close (duplicates, unused items near the deadline,
    captures PayPal reported as reversed). A human confirms or dismisses each proposal; nothing
    is refunded automatically.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from . import procurement as pr
from .ledger import Ledger, from_micro

DAY = 86400.0
SOON = 2 * DAY          # "refund window closes soon"
BUDGET_DAYS = 30


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def _item_name(vendor: str, item: str) -> str:
    return pr.VENDORS.get(vendor, {}).get("items", {}).get(item, {}).get("name", item)


def sweep(ledger: Ledger, aid: str, now: float | None = None) -> list[str]:
    """Expire stale approvals and create refund suggestions. Returns the new suggestion ids."""
    now = now or time.time()
    created = []
    purchases = ledger.purchases(aid, 500)
    for p in purchases:
        if p["status"] == "pending_approval" and p["approval_expires"] and now > p["approval_expires"]:
            ledger.update_purchase(p["id"], status="expired", reason=(p["reason"] or "") + " | approval link expired")
    completed = sorted((p for p in purchases if p["status"] == "completed"), key=lambda p: p["ts"])
    seen: dict[tuple, dict] = {}
    for p in completed:
        open_window = p["refund_deadline"] and now < p["refund_deadline"]
        key = (p["vendor"], p["item"])
        name = _item_name(p["vendor"], p["item"])
        if open_window and p["flag"]:
            sid = ledger.add_suggestion(aid, p["id"], "reversed",
                                        f"PayPal reported this capture as {p['flag']}. Refund or cancel '{name}' "
                                        f"before the window closes so the books match.", p["refund_deadline"])
            created += [sid] if sid else []
        first = seen.get(key)
        if open_window and first and p["ts"] - first["ts"] < 7 * DAY and pr.VENDORS.get(p["vendor"], {}).get("fulfillment") != "live":
            sid = ledger.add_suggestion(aid, p["id"], "duplicate",
                                        f"Looks like a duplicate: '{name}' was already bought "
                                        f"{(p['ts'] - first['ts']) / 3600:.0f} h earlier ({first['id']}). "
                                        f"Refund ${from_micro(p['amount_micro']):.2f} before the window closes?",
                                        p["refund_deadline"])
            created += [sid] if sid else []
        seen.setdefault(key, p)
        if open_window and not p["used_ts"] and p["refund_deadline"] - now < SOON \
                and pr.VENDORS.get(p["vendor"], {}).get("fulfillment") != "live":
            hours = (p["refund_deadline"] - now) / 3600
            sid = ledger.add_suggestion(aid, p["id"], "unused",
                                        f"'{name}' hasn't been marked as used and its refund window closes in "
                                        f"{hours:.0f} h. Refund ${from_micro(p['amount_micro']):.2f}?", p["refund_deadline"])
            created += [sid] if sid else []
    # suggestions whose purchase is no longer refundable go stale
    for sg in ledger.suggestions(aid, "open"):
        pu = ledger.purchase(sg["purchase_id"])
        if not pu or pu["status"] != "completed" or (pu["refund_deadline"] and now > pu["refund_deadline"]):
            ledger.set_suggestion(sg["id"], "stale")
    return created


def build(ledger: Ledger, agent, now: float | None = None) -> dict:
    """Bryntum Scheduler data for one agent: one row for the agent, one per vendor it has touched."""
    now = now or time.time()
    aid = agent["id"]
    cfg = pr.AgentConfig.from_row(agent)
    purchases = ledger.purchases(aid, 500)
    suggestions = ledger.suggestions(aid)
    open_by_purchase = {s["purchase_id"]: s for s in suggestions if s["status"] == "open"}
    vendors = list((cfg.vendor_budgets or {}).keys())
    for p in purchases:
        if p["vendor"] not in vendors:
            vendors.append(p["vendor"])
    resources = [{"id": aid, "name": agent["name"], "kind": "agent",
                  "detail": f"tab ${ledger.balance(aid):.2f}"}]
    events: list[dict] = []
    for vid in vendors:
        v = pr.VENDORS.get(vid, {"name": vid, "category": ""})
        allowed = cfg.vendor_budgets is None or vid in cfg.vendor_budgets
        rid = f"{aid}:{vid}"
        detail = v["category"] if allowed else "not on allowlist"
        resources.append({"id": rid, "name": v["name"], "kind": "vendor", "detail": detail, "agent": aid})
        if allowed and cfg.vendor_budgets is not None:
            budget = cfg.vendor_budgets[vid]
            spent = ledger.vendor_spent_since(aid, vid, now - BUDGET_DAYS * DAY)
            pct = float(spent / budget * 100) if budget else 0
            events.append({"id": f"budget:{rid}", "resourceId": rid, "type": "budget",
                           "name": f"30-day budget: ${spent:.2f} of ${budget:.2f}",
                           "startDate": iso(now - BUDGET_DAYS * DAY), "endDate": iso(now),
                           "percent": round(pct), "eventColor": "red" if pct >= 90 else "orange" if pct >= 60 else "blue"})
    for p in purchases:
        rid = f"{aid}:{p['vendor']}"
        name = _item_name(p["vendor"], p["item"])
        amt = f"${from_micro(p['amount_micro']):.2f}"
        base = {"purchaseId": p["id"], "resourceId": rid, "amount": amt, "status": p["status"],
                "reason": p["reason"] or "", "funding": p["funding"] or ""}
        if p["status"] in ("pending_approval", "expired", "rejected"):
            end = p["approval_expires"] or p["ts"] + DAY
            events.append(base | {"id": f"appr:{p['id']}", "type": "approval",
                                  "name": f"Approval: {name} {amt}" + ("" if p["status"] == "pending_approval" else f" ({p['status']})"),
                                  "startDate": iso(p["ts"]), "endDate": iso(end),
                                  "eventColor": "yellow" if p["status"] == "pending_approval" else "gray"})
        elif p["status"] in ("completed", "refunded"):
            end = p["refund_deadline"] or p["ts"] + 7 * DAY
            if p["status"] == "refunded":
                end = p["refunded_ts"] or end
            closing = p["status"] == "completed" and end - now < SOON
            sg = open_by_purchase.get(p["id"])
            events.append(base | {"id": f"win:{p['id']}", "type": "refund_window",
                                  "name": (f"Refund window: {name} {amt}" if p["status"] == "completed"
                                           else f"Refunded: {name} {amt}"),
                                  "startDate": iso(p["ts"]), "endDate": iso(end),
                                  "eventColor": "purple" if p["status"] == "refunded" else "red" if closing else "green",
                                  "suggestion": sg and {"id": sg["id"], "kind": sg["kind"], "reason": sg["reason"]},
                                  "confirmed": bool(p["paypal_confirmed"]), "flag": p["flag"] or "",
                                  "used": bool(p["used_ts"])})
            renews = pr.VENDORS.get(p["vendor"], {}).get("items", {}).get(p["item"], {}).get("renews_days")
            if renews and p["status"] == "completed":
                at = p["ts"] + renews * DAY
                need = cfg.approval_threshold is not None and from_micro(p["amount_micro"]) > cfg.approval_threshold
                events.append({"id": f"renew:{p['id']}", "resourceId": rid, "type": "renewal", "purchaseId": p["id"],
                               "name": f"Renews: {name} {amt}" + (" (needs approval)" if need else ""),
                               "startDate": iso(at), "endDate": iso(at), "eventColor": "cyan"})
        elif p["status"] == "denied":
            events.append(base | {"id": f"deny:{p['id']}", "type": "denied", "name": f"Blocked: {name} {amt}",
                                  "startDate": iso(p["ts"]), "endDate": iso(p["ts"]), "eventColor": "red"})
    for e in ledger.entries(aid, 200):
        if e["kind"] == "fund" and e["tool"] != "approval":
            events.append({"id": f"fund:{e['id']}", "resourceId": aid, "type": "topup",
                           "name": f"Top-up ${e['amount_usd']:.2f}", "startDate": iso(e["ts"]), "endDate": iso(e["ts"]),
                           "eventColor": "green", "reason": e["reason"] or ""})
        elif e["kind"] == "refund":
            events.append({"id": f"ref:{e['id']}", "resourceId": aid, "type": "paypal_refund",
                           "name": f"PayPal refund ${e['amount_usd']:.2f}", "startDate": iso(e["ts"]),
                           "endDate": iso(e["ts"]), "eventColor": "purple", "reason": e["reason"] or ""})
    for w in ledger.webhooks(aid):
        events.append({"id": f"wh:{w['id']}", "resourceId": aid, "type": "webhook",
                       "name": f"PayPal webhook: {w['type']}" + ("" if w["verified"] else " (unverified)"),
                       "startDate": iso(w["ts"]), "endDate": iso(w["ts"]), "eventColor": "indigo",
                       "reason": w["effect"] or "", "purchaseId": w["purchase_id"] or ""})
    return {"now": iso(now), "agent_id": aid, "resources": resources, "events": events,
            "suggestions": [s for s in suggestions if s["status"] == "open"],
            "history": [s for s in suggestions if s["status"] != "open"][:20]}
