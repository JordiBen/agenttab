"""AgentTab: a PayPal-funded spending tab for AI agents, with an AI spend guard.

Humans fund an agent's tab once through PayPal Checkout (Orders v2). The agent
then buys metered web-intelligence calls on its own. Every purchase passes hard
limits plus an AI check against the owner's plain-English policy. Owners watch
every charge and denial live in an AG Grid dashboard and can refund unused
credit back to PayPal in one click.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from starlette.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from . import llm
from .config import Settings, get_settings
from .guard import SpendGuard
from .ledger import Ledger, from_micro
from .ratelimit import DAY, HOUR, RateLimiter
from .paypal import PayPalClient, PayPalError
from .tools import DESCRIPTIONS, PRICES, TOOLS, UnsafeTarget, check_url
from . import procurement as pr
from . import schedule as sched

log = logging.getLogger("agenttab")
DEMO_POLICY = pr.SHOP_POLICY
STATIC = Path(__file__).resolve().parent.parent / "static"


class NewAgent(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    owner_email: str = ""
    policy: str = Field(min_length=5, max_length=2000)
    daily_cap_usd: float = 2.0
    per_call_cap_usd: float = 0.10          # hard per-purchase maximum
    vendor_budgets: dict[str, float] | None = None   # {vendor_id: rolling 30-day budget}; None = any vendor
    approval_threshold_usd: float | None = None      # above this a human approves via PayPal
    refund_window_days: int = Field(default=7, ge=0, le=180)

class PolicyUpdate(BaseModel):
    policy: str | None = None
    daily_cap_usd: float | None = None
    per_call_cap_usd: float | None = None
    vendor_budgets: dict[str, float] | None = None
    approval_threshold_usd: float | None = None
    refund_window_days: int | None = Field(default=None, ge=0, le=180)

class PurchaseReq(BaseModel):
    vendor: str = Field(max_length=64)
    item: str = Field(max_length=64)
    quantity: int = Field(default=1, ge=1, le=100)
    purpose: str = Field(default="", max_length=500)
    target: str = Field(default="", max_length=2048)

class TopUp(BaseModel):
    amount_usd: float = 5.0

class ToolCall(BaseModel):
    url: str = Field(max_length=2048)
    purpose: str = Field(default="", max_length=500)


def create_app(settings: Settings | None = None, allow_private_targets: bool | None = None) -> FastAPI:
    s = settings or get_settings()
    if allow_private_targets is None:  # local dev only: some sandboxes resolve public hosts to 198.18.x
        allow_private_targets = os.getenv("ALLOW_PRIVATE_TARGETS_FOR_LOCAL_DEV") == "1"
    app = FastAPI(title="AgentTab", version="1.0.0",
                  description="PayPal-funded spending tabs for AI agents, guarded by AI.")
    admin_token = s.admin_token
    if not admin_token:
        admin_token = secrets.token_urlsafe(32)
        tok_file = Path(".admin_token")
        tok_file.write_text(admin_token)
        tok_file.chmod(0o600)
        log.warning("ADMIN_TOKEN not set: generated a random one in %s (chmod 600)", tok_file.resolve())
    if s.demo_mode and (len(admin_token) < 24 or admin_token in ("dev-admin", "admin", "changeme")):
        raise RuntimeError("DEMO_MODE requires a strong ADMIN_TOKEN (24+ random chars)")
    limiter = RateLimiter()
    ledger = Ledger(s.db_path)
    paypal = PayPalClient(s)
    guard = SpendGuard(s, ai_allowed=lambda: limiter.allow("llm", s.llm_calls_per_day, DAY))
    app.state.ledger, app.state.paypal, app.state.guard, app.state.settings = ledger, paypal, guard, s

    def admin(x_admin_token: str = Header("")):
        if not hmac.compare_digest(x_admin_token.encode(), admin_token.encode()):
            raise HTTPException(401, "admin token required")

    def agent_from_key(x_agent_key: str = Header("")):
        a = ledger.agent_by_key(x_agent_key) if x_agent_key else None
        if not a:
            raise HTTPException(401, "valid X-Agent-Key required")
        return a

    def client_ip(request: Request) -> str:
        # Edge-set headers first (Cloudflare in front of Render overwrites these, clients can't spoof them),
        # then the last X-Forwarded-For hop (appended by the proxy), then the socket peer.
        for h in ("cf-connecting-ip", "true-client-ip"):
            if request.headers.get(h):
                return request.headers[h].strip()
        fwd = request.headers.get("x-forwarded-for", "")
        return (fwd.split(",")[-1].strip() if fwd else (request.client.host if request.client else "?"))

    def paypal_budget():
        if not limiter.allow("paypal", s.paypal_calls_per_day, DAY):
            raise HTTPException(429, "daily PayPal sandbox call budget for this demo is used up; try tomorrow")

    def money(v: str | float) -> Decimal:
        try:
            d = Decimal(str(v)).quantize(Decimal("0.01"))
        except InvalidOperation:
            raise HTTPException(422, "bad amount")
        if d <= 0 or d > Decimal("500"):
            raise HTTPException(422, "amount must be between 0.01 and 500")
        return d

    def agent_view(a) -> dict:
        return {"agent_id": a["id"], "name": a["name"], "owner_email": a["owner_email"],
                "policy": a["policy"], "daily_cap_usd": float(from_micro(a["daily_cap_micro"])),
                "per_call_cap_usd": float(from_micro(a["per_call_cap_micro"])),
                "balance_usd": float(ledger.balance(a["id"])),
                "spent_24h_usd": float(ledger.spent_since(a["id"], time.time() - 86400)),
                "approval_threshold_usd": float(from_micro(a["approval_threshold_micro"])) if a["approval_threshold_micro"] is not None else None,
                "refund_window_days": a["refund_window_days"] if a["refund_window_days"] is not None else 7,
                "vendor_budgets": [{"vendor": vid, "name": pr.VENDORS.get(vid, {}).get("name", vid),
                                    "budget_usd": float(b),
                                    "spent_30d_usd": float(ledger.vendor_spent_since(a["id"], vid, time.time() - 30 * 86400))}
                                   for vid, b in (pr.AgentConfig.from_row(a).vendor_budgets or {}).items()]}

    def vendor_budgets_in(d: dict[str, float] | None) -> dict | None:
        if d is None:
            return None
        unknown = [k for k in d if k not in pr.VENDORS]
        if unknown:
            raise HTTPException(422, f"unknown vendor(s): {unknown}; see /api/catalog")
        return {k: Decimal(str(v)).quantize(Decimal("0.01")) for k, v in d.items()}

    # ---------------- public ----------------
    @app.get("/", include_in_schema=False)
    def home():
        return FileResponse(STATIC / "dashboard.html")

    @app.get("/static/schedule.js", include_in_schema=False)
    def schedule_js():
        return FileResponse(STATIC / "schedule.js", media_type="application/javascript")

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "paypal_mode": s.paypal_mode, "ai_guard": llm.enabled(s), "demo_mode": s.demo_mode,
                "models": [s.llm_model, s.llm_fallback_model] if llm.enabled(s) else [],
                **({"demo_policy": DEMO_POLICY} if s.demo_mode else {})}

    @app.get("/api/catalog")
    def catalog():
        """Machine-readable price list so agents can plan purchases."""
        return {"currency": "USD", "tools": [
            {"name": n, "price_usd": float(PRICES[n]), "description": DESCRIPTIONS[n],
             "endpoint": f"/api/tools/{n}", "body": {"url": "https://...", "purpose": "why you need it"}}
            for n in TOOLS],
            "vendors": [{"vendor": vid, "name": v["name"], "category": v["category"], "fulfillment": v["fulfillment"],
                         "items": [{"item": iid, "name": it["name"], "price_usd": float(it["price"])}
                                   for iid, it in v["items"].items()]} for vid, v in pr.VENDORS.items()],
            "purchase_endpoint": {"method": "POST", "path": "/api/purchases",
                                  "body": {"vendor": "samplebox", "item": "ceramic-mug-sample", "quantity": 1,
                                           "purpose": "why the business needs it", "target": "(data items only) https://..."}}}

    # ---------------- owner (admin) ----------------

    @app.post("/api/agents", dependencies=[Depends(admin)])
    def create_agent(body: NewAgent):
        return ledger.create_agent(body.name, body.owner_email, body.policy,
                                   money(body.daily_cap_usd), money(body.per_call_cap_usd),
                                   vendor_budgets_in(body.vendor_budgets),
                                   money(body.approval_threshold_usd) if body.approval_threshold_usd is not None else None,
                                   body.refund_window_days)

    @app.get("/api/agents", dependencies=[Depends(admin)])
    def list_agents():
        return [agent_view(a) for a in ledger.agents()]


    @app.patch("/api/agents/{aid}", dependencies=[Depends(admin)])
    def update_agent(aid: str, body: PolicyUpdate):
        if not ledger.agent(aid):
            raise HTTPException(404, "no such agent")
        ledger.update_policy(aid, body.policy,
                             money(body.daily_cap_usd) if body.daily_cap_usd is not None else None,
                             money(body.per_call_cap_usd) if body.per_call_cap_usd is not None else None)
        if body.vendor_budgets is not None or body.approval_threshold_usd is not None or body.refund_window_days is not None:
            ledger.set_agent_procurement(aid, vendor_budgets_in(body.vendor_budgets),
                                         money(body.approval_threshold_usd) if body.approval_threshold_usd is not None else None,
                                         body.refund_window_days)
        return agent_view(ledger.agent(aid))

    @app.get("/api/admin/client-ip", dependencies=[Depends(admin)])
    def admin_client_ip(request: Request):
        """Owner diagnostic: which IP the rate limiter sees (header names only + resolved IP)."""
        return {"resolved": client_ip(request),
                "ip_headers": [h for h in request.headers if "ip" in h or "forwarded" in h]}

    @app.get("/api/ledger", dependencies=[Depends(admin)])
    def get_ledger(agent_id: str | None = None):
        return ledger.entries(agent_id)

    @app.post("/api/agents/{aid}/refund", dependencies=[Depends(admin)])
    def refund(aid: str):
        """Return unused credit to the payer's PayPal, newest captures first."""
        if not ledger.agent(aid):
            raise HTTPException(404, "no such agent")
        return do_refund(aid)

    def do_refund(aid: str):
        remaining = ledger.balance(aid)
        if remaining <= 0:
            return {"refunded_usd": 0.0, "refunds": []}
        out = []
        for cap in ledger.captures(aid):
            if remaining <= 0:
                break
            amt = min(remaining, from_micro(cap["amount_micro"])).quantize(Decimal("0.01"), rounding="ROUND_DOWN")
            if amt <= 0:
                break
            paypal_budget()
            try:
                r = paypal.refund_capture(cap["paypal_ref"], amt)
            except PayPalError as e:
                raise HTTPException(502, str(e))
            ledger.add_entry(aid, "refund", amt, paypal_ref=r["id"], reason="unused credit returned")
            out.append({"capture": cap["paypal_ref"], "refund": r["id"], "amount_usd": float(amt)})
            remaining -= amt
        return {"refunded_usd": sum(x["amount_usd"] for x in out), "refunds": out}

    @app.get("/api/agents/{aid}/report", dependencies=[Depends(admin)])
    def report(aid: str):
        """Plain-English spend report for the owner (AI-written when an LLM is configured)."""
        if not ledger.agent(aid):
            raise HTTPException(404, "no such agent")
        return do_report(aid)

    def do_report(aid: str):
        a = ledger.agent(aid)
        rows = ledger.entries(aid, 200)
        charges = [r for r in rows if r["kind"] == "charge"]
        denies = [r for r in rows if r["kind"] == "deny"]
        facts = {"agent": a["name"], "policy": a["policy"], "balance_usd": float(ledger.balance(aid)),
                 "charges": len(charges), "spent_usd": round(sum(r["amount_usd"] for r in charges), 4),
                 "denied": [{"tool": d["tool"], "target": d["target"], "reason": d["reason"]} for d in denies[:10]],
                 "top_targets": sorted({c["target"] for c in charges})[:10],
                 "purchases": [{"vendor": p["vendor"], "item": p["item"], "usd": p["amount_usd"], "status": p["status"]}
                               for p in ledger.purchases(aid, 30)]}
        text, model = None, None
        if llm.enabled(s) and limiter.allow("llm", s.llm_calls_per_day, DAY):
            try:
                text, model = llm.chat(s, [
                    {"role": "system", "content": "Write a 4-sentence spending report for the owner of an AI agent. Flag anything risky. Plain English, no markdown."},
                    {"role": "user", "content": json.dumps(facts)}], temperature=0.2)
                text = text.strip() or None
            except llm.LLMError:
                text = None
        if not text:
            text = (f"{a['name']} made {facts['charges']} paid calls for ${facts['spent_usd']:.2f} and has "
                    f"${facts['balance_usd']:.2f} left. {len(denies)} purchase(s) were blocked by the guard"
                    + (f", most recently: {denies[0]['reason']}." if denies else "."))
        return {"report": text, "facts": facts, "model": model if text and model else "template"}

    # ---------------- funding via PayPal ----------------

    def _topup(aid: str, amount: Decimal):
        paypal_budget()
        try:
            o = paypal.create_order(amount, aid, f"AgentTab credit for {aid}")
        except PayPalError as e:
            raise HTTPException(502, str(e))
        ledger.add_order(o["id"], aid, amount)
        return {"order_id": o["id"], "approve_url": o["approve_url"], "amount_usd": float(amount),
                "next": "A human opens approve_url and pays with PayPal; credit lands instantly on return."}

    @app.post("/api/agents/{aid}/topup", dependencies=[Depends(admin)])
    def owner_topup(aid: str, body: TopUp):
        if not ledger.agent(aid):
            raise HTTPException(404, "no such agent")
        return _topup(aid, money(body.amount_usd))

    @app.post("/api/agents/{aid}/topup-sandbox-card", dependencies=[Depends(admin)])
    def sandbox_card_topup(aid: str, body: TopUp):
        """Sandbox/demo only: fund instantly with PayPal's test card (create+capture, no browser step)."""
        if not ledger.agent(aid):
            raise HTTPException(404, "no such agent")
        if not (paypal.mock or paypal.is_sandbox):
            raise HTTPException(403, "only available in PayPal sandbox")
        amount = money(body.amount_usd)
        paypal_budget()
        try:
            cap = paypal.sandbox_card_topup(amount, aid)
        except PayPalError as e:
            raise HTTPException(502, str(e))
        ledger.add_order(cap["order_id"], aid, amount)
        ledger.mark_order(cap["order_id"], "CAPTURED")
        ledger.add_entry(aid, "fund", cap["amount"], paypal_ref=cap["capture_id"],
                         reason=f"PayPal order {cap['order_id']} (sandbox test card)")
        return {"order_id": cap["order_id"], "capture_id": cap["capture_id"], "amount_usd": float(cap["amount"]),
                "balance_usd": float(ledger.balance(aid))}

    @app.post("/api/me/topup-request")
    def agent_topup_request(body: TopUp, a=Depends(agent_from_key)):
        """The agent asks its human for more budget: returns a PayPal approval link to forward."""
        return _topup(a["id"], money(body.amount_usd))

    @app.get("/paypal/return", include_in_schema=False)
    def paypal_return(token: str):
        kind, ref = capture_order_flow(token)
        return RedirectResponse(("/?approved=" if kind == "approved" else "/?funded=") + ref)

    def capture_order_flow(token: str) -> tuple[str, str]:
        """Capture an approved order once (return URL or CHECKOUT.ORDER.APPROVED webhook, whichever comes first)."""
        o = ledger.order(token)
        if not o:
            raise HTTPException(404, "unknown order")
        pu = ledger.purchase_by_order(token)
        if o["status"] == "CAPTURED":
            return ("approved", pu["id"]) if pu else ("funded", o["agent_id"])
        if o["status"] in ("CANCELLED", "SUPERSEDED"):
            raise HTTPException(409, f"order is {o['status'].lower()}")
        paypal_budget()
        try:
            cap = paypal.capture_order(token)
        except PayPalError as e:
            raise HTTPException(402, f"capture failed: {e}")
        if cap["status"] != "COMPLETED":
            raise HTTPException(402, f"capture status {cap['status']}")
        ledger.mark_order(token, "CAPTURED")
        if pu:  # this order was the approval link for one purchase
            complete_approved(pu, cap["capture_id"], cap["amount"], f"PayPal order {token}")
            sched.sweep(ledger, pu["agent_id"])
            return "approved", pu["id"]
        ledger.add_entry(o["agent_id"], "fund", cap["amount"], paypal_ref=cap["capture_id"],
                         reason=f"PayPal order {token}")
        return "funded", o["agent_id"]

    @app.get("/paypal/cancel", include_in_schema=False)
    def paypal_cancel(token: str = ""):
        if token and ledger.order(token):
            ledger.mark_order(token, "CANCELLED")
        return RedirectResponse("/?cancelled=1")

    @app.get("/mock-paypal/approve/{oid}", include_in_schema=False)
    def mock_approve(oid: str):
        if not paypal.mock:
            raise HTTPException(404)
        paypal.mock_approve(oid)
        return HTMLResponse(f"""<html><body style="font-family:sans-serif;padding:40px">
        <h2>Mock PayPal checkout</h2><p>Order <b>{oid}</b> approved (offline demo mode).</p>
        <a href="/paypal/return?token={oid}">Return to AgentTab</a>
        <script>setTimeout(()=>location.href='/paypal/return?token={oid}',800)</script></body></html>""")

    # ---------------- agent side ----------------
    @app.get("/api/me")
    def me(a=Depends(agent_from_key)):
        return agent_view(a)


    def fulfill(p: pr.Purchase):
        v = pr.VENDORS[p.vendor]
        if v["fulfillment"] == "live":
            return TOOLS[p.item](p.target)
        return {"vendor_order": "SIM-" + secrets.token_hex(4).upper(), "vendor": v["name"],
                "item": v["items"][p.item]["name"], "quantity": p.quantity,
                "note": "sandbox vendor: fulfillment is simulated"}

    def state_for(aid: str) -> pr.State:
        now = time.time()
        return pr.State(ledger.spent_since(aid, now - 86400),
                        {vid: ledger.vendor_spent_since(aid, vid, now - 30 * 86400) for vid in pr.VENDORS})

    def refund_deadline(a) -> float:
        days = a["refund_window_days"] if a["refund_window_days"] is not None else 7
        return time.time() + days * 86400

    def do_purchase(a, p: pr.Purchase, request: Request):
        if not limiter.allow("tools:" + client_ip(request), s.tool_calls_per_ip_hour, HOUR):
            raise HTTPException(429, "too many purchases from your IP; try again later")
        amount = pr.price_of(p)
        if amount is None:
            raise HTTPException(404, "unknown vendor, item or quantity; see /api/catalog")
        if p.target:
            try:
                check_url(p.target, allow_private=allow_private_targets)
            except UnsafeTarget as e:
                raise HTTPException(422, str(e))
        aid = a["id"]
        cfg = pr.AgentConfig.from_row(a)
        needs_approval = cfg.approval_threshold is not None and amount > cfg.approval_threshold
        balance = ledger.balance(aid)
        if not needs_approval and amount > balance:  # check before spending an LLM call
            return JSONResponse(status_code=402, headers={"X-AgentTab-Price": str(amount)}, content={
                "error": "payment_required", "price_usd": float(amount), "balance_usd": float(balance),
                "topup": {"method": "POST", "endpoint": "/api/me/topup-request", "body": {"amount_usd": 5.0},
                          "note": "Returns a PayPal approval link for your human owner."}})
        with ledger.lock:
            st = state_for(aid)
        out = pr.decide(cfg, p, st, "ai", guard, allow_private_targets)
        tag = f"[{out.layer}] {out.reason}"
        if out.outcome == "block":
            ledger.add_entry(aid, "deny", Decimal(0), f"{p.vendor}:{p.item}", p.target, p.purpose, "deny", tag)
            ledger.add_purchase(aid, p.vendor, p.item, p.quantity, amount, p.target, p.purpose, "denied", "",
                                None, None, out.layer, out.reason)
            raise HTTPException(403, {"error": "denied_by_guard", **out.as_dict()})
        if out.outcome == "approval":
            paypal_budget()
            try:
                o = paypal.create_order(amount, aid, f"Approve: {pr.VENDORS[p.vendor]['name']} {p.item} x{p.quantity}")
            except PayPalError as e:
                raise HTTPException(502, str(e))
            ledger.add_order(o["id"], aid, amount)
            expires = time.time() + 86400
            pid = ledger.add_purchase(aid, p.vendor, p.item, p.quantity, amount, p.target, p.purpose,
                                      "pending_approval", "direct", None, expires, out.layer, out.reason,
                                      paypal_order=o["id"])
            ledger.add_entry(aid, "approval", Decimal(0), f"{p.vendor}:{p.item}", p.target, p.purpose,
                             "approval", tag, o["id"])
            return JSONResponse(status_code=202, content={
                "status": "approval_required", "purchase_id": pid, "amount_usd": float(amount),
                "approve_url": o["approve_url"], "approval_expires_at": expires, "guard": out.as_dict(),
                "note": "Forward approve_url to your human. Paying it approves and funds this one purchase."})
        try:
            result = fulfill(p)
        except Exception as e:  # failed calls are never charged
            raise HTTPException(502, f"vendor failed, not charged: {type(e).__name__}")
        deadline = refund_deadline(a)
        pid = ledger.add_purchase(aid, p.vendor, p.item, p.quantity, amount, p.target, p.purpose, "completed",
                                  "tab", deadline, None, out.layer, out.reason, fulfillment=json.dumps(result)[:4000])
        ledger.add_entry(aid, "charge", amount, f"{p.vendor}:{p.item}", p.target, p.purpose, "allow", tag, pid)
        return {"purchase_id": pid, "charged_usd": float(amount), "balance_usd": float(ledger.balance(aid)),
                "refund_deadline": deadline, "guard": out.as_dict(), "result": result}

    def complete_approved(pu, capture_id: str, amount: Decimal, ref: str):
        pu = ledger.purchase(pu["id"])
        if pu["status"] != "pending_approval":      # already handled (return URL, webhook or card)
            return
        aid = pu["agent_id"]
        p = pr.Purchase(pu["vendor"], pu["item"], pu["quantity"], pu["purpose"], pu["target"])
        ledger.add_entry(aid, "fund", amount, "approval", paypal_ref=capture_id, reason=f"approval for {pu['id']}: {ref}")
        try:
            result = fulfill(p)
        except Exception as e:
            paypal_budget()
            paypal.refund_capture(capture_id, amount)
            ledger.add_entry(aid, "refund", amount, "approval", reason=f"vendor failed ({type(e).__name__}); auto-refunded")
            ledger.update_purchase(pu["id"], status="failed_refunded", paypal_capture=capture_id)
            return
        a = ledger.agent(aid)
        ledger.add_entry(aid, "charge", amount, f"{p.vendor}:{p.item}", p.target, p.purpose, "allow",
                         f"[approval] approved by owner via PayPal ({ref})", pu["id"])
        ledger.update_purchase(pu["id"], status="completed", paypal_capture=capture_id,
                               refund_deadline=refund_deadline(a), fulfillment=json.dumps(result)[:4000])

    def refund_one(pu, note: str = "") -> dict:
        pid = pu["id"]
        pu = ledger.purchase(pid)
        if pu["status"] != "completed":
            raise HTTPException(409, f"purchase is {pu['status']}, not refundable")
        if pu["refund_deadline"] is None or time.time() > pu["refund_deadline"]:
            raise HTTPException(409, "refund window has closed")
        amount = from_micro(pu["amount_micro"])
        aid = pu["agent_id"]
        why = f"purchase {pid} refunded" + (f" ({note})" if note else "")
        if pu["funding"] == "direct":
            paypal_budget()
            try:
                r = paypal.refund_capture(pu["paypal_capture"], amount)
            except PayPalError as e:
                raise HTTPException(502, str(e))
            ledger.add_entry(aid, "credit", amount, f"{pu['vendor']}:{pu['item']}", reason=why)
            ledger.add_entry(aid, "refund", amount, "approval", paypal_ref=r["id"],
                             reason=f"{why}: PayPal capture {pu['paypal_capture']} refunded")
            ref = r["id"]
        else:
            ledger.add_entry(aid, "credit", amount, f"{pu['vendor']}:{pu['item']}", reason=why + " to tab")
            ref = None
        ledger.update_purchase(pid, status="refunded", refunded_ts=time.time())
        return {"purchase_id": pid, "refunded_usd": float(amount), "to": "paypal" if ref else "tab",
                "paypal_refund": ref, "balance_usd": float(ledger.balance(aid))}

    def purchase_for(pid: str, x_admin_token: str, x_agent_key: str, demo_owner_ok: bool):
        pu = ledger.purchase(pid)
        if not pu:
            raise HTTPException(404, "no such purchase")
        if x_admin_token and hmac.compare_digest(x_admin_token.encode(), admin_token.encode()):
            return pu
        a = ledger.agent_by_key(x_agent_key) if x_agent_key else None
        if a and a["id"] == pu["agent_id"] and demo_owner_ok:
            return pu
        raise HTTPException(401, "admin token (or, in demo mode, the demo agent key) required")

    @app.post("/api/tools/{name}")
    def call_tool(name: str, body: ToolCall, request: Request, a=Depends(agent_from_key)):
        if name not in TOOLS:
            raise HTTPException(404, "unknown tool; see /api/catalog")
        return do_purchase(a, pr.Purchase("webintel-data", name, 1, body.purpose, body.url), request)

    @app.post("/api/purchases")
    def create_purchase(body: PurchaseReq, request: Request, a=Depends(agent_from_key)):
        """Agent buys from a vendor. 200 = bought from the tab, 202 = needs owner approval (PayPal link),
        402 = tab empty, 403 = blocked by the guard."""
        return do_purchase(a, pr.Purchase(body.vendor, body.item, body.quantity, body.purpose, body.target), request)

    @app.get("/api/me/purchases")
    def my_purchases(a=Depends(agent_from_key)):
        return ledger.purchases(a["id"])

    @app.get("/api/purchases", dependencies=[Depends(admin)])
    def list_purchases(agent_id: str):
        return ledger.purchases(agent_id)

    @app.post("/api/purchases/{pid}/refund")
    def refund_purchase(pid: str, x_admin_token: str = Header(""), x_agent_key: str = Header("")):
        """Refund one purchase inside its refund window. Tab-funded: credit returns to the tab.
        Approval-funded: the purchase's own PayPal capture is refunded."""
        pu = purchase_for(pid, x_admin_token, x_agent_key, demo_owner_ok=s.demo_mode)
        return refund_one(pu)

    @app.post("/api/purchases/{pid}/approve-sandbox-card")
    def approve_sandbox_card(pid: str, x_admin_token: str = Header(""), x_agent_key: str = Header("")):
        """Owner approves a pending purchase and pays it with PayPal's sandbox test card (no browser step).
        In demo mode the visitor plays the owner of their own demo agent."""
        pu = purchase_for(pid, x_admin_token, x_agent_key, demo_owner_ok=s.demo_mode)
        if pu["status"] != "pending_approval":
            raise HTTPException(409, f"purchase is {pu['status']}")
        if time.time() > (pu["approval_expires"] or 0):
            ledger.update_purchase(pid, status="expired")
            raise HTTPException(409, "approval has expired")
        if not (paypal.mock or paypal.is_sandbox):
            raise HTTPException(403, "only available in PayPal sandbox")
        amount = from_micro(pu["amount_micro"])
        paypal_budget()
        try:
            cap = paypal.sandbox_card_topup(amount, pu["agent_id"])
        except PayPalError as e:
            raise HTTPException(502, str(e))
        if pu["paypal_order"]:
            ledger.mark_order(pu["paypal_order"], "SUPERSEDED")
        complete_approved(pu, cap["capture_id"], cap["amount"], f"PayPal order {cap['order_id']} (sandbox test card)")
        pu = ledger.purchase(pid)
        return {"purchase_id": pid, "status": pu["status"], "paypal_order_id": cap["order_id"],
                "paypal_capture_id": cap["capture_id"], "refund_deadline": pu["refund_deadline"]}

    @app.post("/api/purchases/{pid}/reject")
    def reject_purchase(pid: str, x_admin_token: str = Header(""), x_agent_key: str = Header("")):
        pu = purchase_for(pid, x_admin_token, x_agent_key, demo_owner_ok=s.demo_mode)
        if pu["status"] != "pending_approval":
            raise HTTPException(409, f"purchase is {pu['status']}")
        ledger.update_purchase(pid, status="rejected")
        if pu["paypal_order"]:
            ledger.mark_order(pu["paypal_order"], "CANCELLED")
        return {"purchase_id": pid, "status": "rejected"}

    # ---------------- PayPal webhooks (sandbox) ----------------
    def _capture_of_refund(res: dict) -> str:
        for l in res.get("links", []):
            if l.get("rel") == "up" and "/captures/" in l.get("href", ""):
                return l["href"].rstrip("/").rsplit("/", 1)[1]
        return ""

    def handle_webhook(typ: str, res: dict) -> tuple[str, str, str]:
        """Apply one PayPal event to the ledger. Returns (agent_id, purchase_id, effect)."""
        rid = str(res.get("id", ""))
        if typ == "CHECKOUT.ORDER.APPROVED":
            o = ledger.order(rid)
            if not o:
                return "", "", "order not created by AgentTab; ignored"
            pu = ledger.purchase_by_order(rid)
            if o["status"] == "CAPTURED":
                return o["agent_id"], pu["id"] if pu else "", "already captured via return URL"
            try:
                kind, ref = capture_order_flow(rid)
            except HTTPException as e:
                return o["agent_id"], pu["id"] if pu else "", f"capture failed: {e.detail}"
            return o["agent_id"], pu["id"] if pu else "", ("approved purchase completed" if kind == "approved"
                                                         else "top-up captured") + " from the webhook"
        if typ.startswith("PAYMENT.CAPTURE."):
            amount = Decimal(str((res.get("amount") or {}).get("value", "0")))
            if typ == "PAYMENT.CAPTURE.REFUNDED":
                refund_id, cid = rid, _capture_of_refund(res)
                known = ledger.refund_entry_by_ref(refund_id)
                if known:
                    pu = ledger.purchase_by_capture(cid) if cid else None
                    return known["agent_id"], pu["id"] if pu else "", f"refund {refund_id} confirmed by PayPal"
                pu = ledger.purchase_by_capture(cid) if cid else None
                if pu and pu["status"] == "completed":
                    aid = pu["agent_id"]
                    ledger.add_entry(aid, "credit", from_micro(pu["amount_micro"]), f"{pu['vendor']}:{pu['item']}",
                                     reason=f"purchase {pu['id']} refunded in PayPal")
                    ledger.add_entry(aid, "refund", amount, "approval", paypal_ref=refund_id,
                                     reason=f"refund made outside AgentTab on capture {cid}")
                    ledger.update_purchase(pu["id"], status="refunded", refunded_ts=time.time())
                    return aid, pu["id"], "refunded outside AgentTab; purchase marked refunded"
                fe = ledger.fund_entry_by_capture(cid) if cid else None
                if fe:
                    ledger.add_entry(fe["agent_id"], "refund", amount, paypal_ref=refund_id,
                                     reason=f"top-up capture {cid} refunded in PayPal")
                    return fe["agent_id"], "", f"top-up refunded in PayPal; tab reduced by ${amount:.2f}"
                return "", "", "refund for an unknown capture; ignored"
            order_id = ((res.get("supplementary_data") or {}).get("related_ids") or {}).get("order_id", "")
            pu = ledger.purchase_by_capture(rid) or (ledger.purchase_by_order(order_id) if order_id else None)
            fe = ledger.fund_entry_by_capture(rid)
            if typ == "PAYMENT.CAPTURE.COMPLETED":
                if pu:
                    if pu["status"] == "pending_approval":
                        if order_id and ledger.order(order_id):
                            ledger.mark_order(order_id, "CAPTURED")
                        complete_approved(pu, rid, amount, f"PayPal order {order_id} (webhook)")
                    ledger.update_purchase(pu["id"], paypal_confirmed=time.time())
                    return pu["agent_id"], pu["id"], "capture confirmed by PayPal"
                if fe:
                    return fe["agent_id"], "", "top-up capture confirmed by PayPal"
                return "", "", "capture for an unknown order; ignored"
            if typ in ("PAYMENT.CAPTURE.DENIED", "PAYMENT.CAPTURE.REVERSED"):
                flag = typ.rsplit(".", 1)[1].lower()
                if pu:
                    ledger.update_purchase(pu["id"], flag=flag)
                    return pu["agent_id"], pu["id"], f"capture {flag}; purchase flagged for review"
                if fe:
                    ledger.add_entry(fe["agent_id"], "refund", amount, paypal_ref=rid,
                                     reason=f"top-up capture {flag} by PayPal; tab reduced")
                    return fe["agent_id"], "", f"top-up capture {flag}; tab reduced by ${amount:.2f}"
                return "", "", f"capture {flag} for an unknown order"
            if typ == "PAYMENT.CAPTURE.PENDING":
                return (pu["agent_id"] if pu else fe["agent_id"] if fe else ""), pu["id"] if pu else "", "capture pending at PayPal"
        return "", "", "event type not used"

    @app.post("/paypal/webhook", include_in_schema=False)
    async def paypal_webhook(request: Request):
        raw = await request.body()
        if len(raw) > 64_000:
            raise HTTPException(413, "too large")
        if not limiter.allow("webhook:all", 3000, DAY):
            raise HTTPException(429, "webhook limit")
        try:
            event = json.loads(raw)
            eid, typ, res = str(event["id"]), str(event["event_type"]), event.get("resource") or {}
        except Exception:
            raise HTTPException(400, "bad event")
        if paypal.mock:
            verified = False                   # offline mode: no PayPal to verify against
        else:
            if not s.paypal_webhook_id:
                raise HTTPException(503, "PAYPAL_WEBHOOK_ID not configured")
            try:
                verified = await run_in_threadpool(paypal.verify_webhook, dict(request.headers), event, s.paypal_webhook_id)
            except PayPalError:
                verified = False
            if not verified:
                raise HTTPException(400, "webhook signature not verified")
        if ledger.webhook_seen(eid):
            return {"ok": True, "duplicate": True}
        aid, pid, effect = await run_in_threadpool(handle_webhook, typ, res)
        ledger.add_webhook(eid, typ, str(res.get("id", "")), aid, pid, verified, effect)
        if aid:
            sched.sweep(ledger, aid)
        log.info("webhook %s %s -> %s", typ, eid, effect)
        return {"ok": True, "effect": effect}

    @app.get("/api/webhooks", dependencies=[Depends(admin)])
    def list_webhooks(agent_id: str = ""):
        return ledger.webhooks(agent_id or None)

    # ---------------- schedule (Bryntum Scheduler) + refund suggestions ----------------
    @app.get("/api/me/schedule")
    def my_schedule(a=Depends(agent_from_key)):
        sched.sweep(ledger, a["id"])
        return sched.build(ledger, ledger.agent(a["id"]))

    @app.get("/api/schedule", dependencies=[Depends(admin)])
    def agent_schedule(agent_id: str):
        a = ledger.agent(agent_id)
        if not a:
            raise HTTPException(404, "no such agent")
        sched.sweep(ledger, agent_id)
        return sched.build(ledger, a)

    def suggestion_for(sid: str, x_admin_token: str, x_agent_key: str):
        sg = ledger.suggestion(sid)
        if not sg:
            raise HTTPException(404, "no such suggestion")
        purchase_for(sg["purchase_id"], x_admin_token, x_agent_key, demo_owner_ok=s.demo_mode)
        if sg["status"] != "open":
            raise HTTPException(409, f"suggestion is {sg['status']}")
        return sg

    @app.post("/api/suggestions/{sid}/confirm")
    def confirm_suggestion(sid: str, x_admin_token: str = Header(""), x_agent_key: str = Header("")):
        """A human confirms a refund the scheduling agent proposed. Only now does money move."""
        sg = suggestion_for(sid, x_admin_token, x_agent_key)
        out = refund_one(ledger.purchase(sg["purchase_id"]), note=f"{sg['kind']} refund suggested by the scheduler, confirmed by owner")
        ledger.set_suggestion(sid, "confirmed")
        return out | {"suggestion_id": sid, "status": "confirmed"}

    @app.post("/api/suggestions/{sid}/dismiss")
    def dismiss_suggestion(sid: str, x_admin_token: str = Header(""), x_agent_key: str = Header("")):
        suggestion_for(sid, x_admin_token, x_agent_key)
        ledger.set_suggestion(sid, "dismissed")
        return {"suggestion_id": sid, "status": "dismissed"}

    @app.post("/api/purchases/{pid}/used")
    def mark_used(pid: str, x_admin_token: str = Header(""), x_agent_key: str = Header("")):
        """The agent (or owner) records that an item was actually used, so it isn't proposed for refund."""
        pu = purchase_for(pid, x_admin_token, x_agent_key, demo_owner_ok=True)
        ledger.update_purchase(pid, used_ts=time.time())
        return {"purchase_id": pid, "used": True, "status": pu["status"]}

    @app.post("/api/demo/fast-forward")
    def demo_fast_forward(request: Request, days: float = 6, a=Depends(agent_from_key)):
        """Demo clock: move this demo agent's history N days into the past (max 6 per call) so refund
        deadlines approach and the scheduler's suggestions appear. Demo mode only; touches only your agent."""
        if not s.demo_mode:
            raise HTTPException(404, "demo mode is off")
        if not (0 < days <= 6):
            raise HTTPException(422, "days must be between 0 and 6")
        if not limiter.allow("ff:" + client_ip(request), 30, HOUR):
            raise HTTPException(429, "slow down")
        ledger.update_entries_ts(a["id"], -days * 86400)
        new = sched.sweep(ledger, a["id"])
        return {"shifted_days": days, "new_suggestions": len(new)}

    # ---------------- public sandboxed demo ----------------
    @app.post("/api/demo/session")
    def demo_session(request: Request):
        """Anyone can start a demo: a fresh agent with a fixed policy and tight caps,
        funded with $1.00 of PayPal *sandbox* money (test card, real sandbox capture)."""
        if not s.demo_mode:
            raise HTTPException(404, "demo mode is off")
        ip = client_ip(request)
        if not limiter.allow("demo:" + ip, s.demo_sessions_per_ip_hour, HOUR):
            raise HTTPException(429, "demo session limit for your IP reached; try again in an hour")
        if not limiter.allow("demo:all", s.demo_sessions_per_day, DAY):
            raise HTTPException(429, "today's demo sessions are used up; try tomorrow")
        ag = ledger.create_agent("demo-" + secrets.token_hex(3), "", DEMO_POLICY, pr.SHOP_DAILY_CAP,
                                 pr.SHOP_PER_PURCHASE_MAX, dict(pr.SHOP_VENDOR_BUDGETS),
                                 pr.SHOP_APPROVAL_THRESHOLD, pr.SHOP_REFUND_WINDOW_DAYS)
        paypal_budget()
        try:
            cap = paypal.sandbox_card_topup(Decimal("1.00"), ag["agent_id"])
        except PayPalError as e:
            raise HTTPException(502, f"PayPal sandbox top-up failed: {e}")
        ledger.add_order(cap["order_id"], ag["agent_id"], Decimal("1.00"))
        ledger.mark_order(cap["order_id"], "CAPTURED")
        ledger.add_entry(ag["agent_id"], "fund", cap["amount"], paypal_ref=cap["capture_id"],
                         reason=f"PayPal sandbox order {cap['order_id']} (demo, test card)")
        return {"agent_key": ag["api_key"], **agent_view(ledger.agent(ag["agent_id"])),
                "paypal_order_id": cap["order_id"], "paypal_capture_id": cap["capture_id"]}

    @app.get("/api/demo/ledger")
    def demo_ledger(a=Depends(agent_from_key)):
        return ledger.entries(a["id"], 200)

    @app.get("/api/demo/purchases")
    def demo_purchases(a=Depends(agent_from_key)):
        return ledger.purchases(a["id"])

    @app.get("/api/scorecard")
    def scorecard():
        """Safety eval results (eval/results.json, committed). Served from file: no LLM or PayPal calls."""
        f = Path(__file__).resolve().parent.parent / "eval" / "results.json"
        if not f.exists():
            raise HTTPException(404, "no eval results yet")
        j = json.loads(f.read_text())
        return {k: j[k] for k in ("generated_at", "cases", "attacks", "legit", "persona", "modes", "by_category", "notes") if k in j}

    @app.post("/api/demo/refund")
    def demo_refund(a=Depends(agent_from_key)):
        return do_refund(a["id"])

    @app.get("/api/demo/report")
    def demo_report(a=Depends(agent_from_key)):
        return do_report(a["id"])

    return app


app = create_app()
