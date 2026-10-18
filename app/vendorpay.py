"""Vendor payouts, capture-only (docs/vendor-payouts-impl-plan.md), behind VENDOR_PAYOUTS=1.

Merchant ids come only from the environment; they are never written to git, logs or API responses.
A vendor without a valid id is never paid, and nothing falls back to AgentTab's own PayPal account.
PayPal-Request-Ids are derived from the purchase id, so a retry or a reconcile replay returns the first result."""
from __future__ import annotations

import os
import re

# Only these vendors can ever be paid. Forbidden vendors (followers, gift cards) have no entry on purpose.
PAYEE_ENV = {"samplebox": "VENDOR_SAMPLEBOX_MERCHANT_ID",
             "shopsuite": "VENDOR_SHOPSUITE_MERCHANT_ID",
             "sitescope-data": "VENDOR_SITESCOPE_MERCHANT_ID"}
_MERCHANT = re.compile(r"[A-Z0-9]{13}")
_PID = re.compile(r"pu_[0-9a-f]{12}")
ACTIONS = ("pay",)


def valid_merchant_id(v) -> bool:
    return isinstance(v, str) and _MERCHANT.fullmatch(v) is not None


def merchant_id(vendor: str) -> str | None:
    name = PAYEE_ENV.get(vendor)
    if not name:
        return None
    v = os.environ.get(name, "")
    return v if valid_merchant_id(v) else None


def payees() -> dict:
    return {v: merchant_id(v) for v in PAYEE_ENV}


def payable_count() -> int:
    return sum(1 for m in payees().values() if m)


def request_id(pid: str, action: str) -> str:
    """`at-{pid}-{action}`: deterministic per (purchase, action), never random, at most 38 characters."""
    if not isinstance(pid, str) or not _PID.fullmatch(pid):
        raise ValueError("bad purchase id")
    if action not in ACTIONS:
        raise ValueError("unknown action")
    rid = f"at-{pid}-{action}"
    assert len(rid) <= 38
    return rid


# A definite refusal: PayPal answered and made no payment (card declined, bad request, not allowed). Anything else
# (no answer, 5xx, 409 for a simultaneous duplicate Request-Id, 429) is unknown: retried once with the same id.
# 401 is our own OAuth token expiring, not a refusal: PayPalClient.pay_vendor refreshes the token and retries once
# with the same Request-Id; only a 401 on that retry (marked auth_retried) counts as a refusal.
DEFINITE_REFUSAL = frozenset({400, 403, 404, 422})


def definite_refusal(e) -> bool:
    st = getattr(e, "status", 0)
    return st in DEFINITE_REFUSAL or (st == 401 and getattr(e, "auth_retried", False) is True)


def check_payment(j: dict, amount_cents: int, payee_merchant_id: str, pid: str) -> tuple[str, dict]:
    """Check PayPal's create-order answer before calling a purchase paid. Returns ("completed" | "payment_pending" |
    "flagged", {order_id, capture_id, reason}). Reasons never contain a merchant id."""
    from .ledger import parse_cents
    info = {"order_id": "", "capture_id": "", "reason": ""}

    def flag(why: str):
        return "flagged", dict(info, reason=why)

    def amount_ok(a) -> bool:
        return isinstance(a, dict) and a.get("currency_code") == "USD" and parse_cents(a.get("value")) == amount_cents
    try:
        info["order_id"] = str(j.get("id") or "")[:64]
        units = j.get("purchase_units") or []
        if len(units) != 1:
            return flag("expected exactly one purchase unit")
        u = units[0]
        pay = u.get("payments") or {}
        caps = pay.get("captures") or []
        if len(caps) == 1:
            info["capture_id"] = str(caps[0].get("id") or "")[:64]
        if (u.get("payee") or {}).get("merchant_id") != payee_merchant_id:
            return flag("payee in PayPal's answer is not the configured vendor")
        if u.get("custom_id") != pid:
            return flag("custom_id in PayPal's answer is not this purchase")
        if not amount_ok(u.get("amount")):
            return flag("order amount differs from the reservation")
        if pay.get("authorizations"):
            return flag("unexpected authorization (a hold) in PayPal's answer")
        if len(caps) != 1 or not info["capture_id"]:
            return flag(f"expected exactly one capture, got {len(caps)}")
        cap = caps[0]
        if "payee" in cap and (cap.get("payee") or {}).get("merchant_id") != payee_merchant_id:
            return flag("capture payee is not the configured vendor")
        if not amount_ok(cap.get("amount")):
            return flag("captured amount differs from the reservation")
        if j.get("status") != "COMPLETED":
            return flag(f"order status {str(j.get('status'))[:30]}")
        st = cap.get("status")
        if st == "COMPLETED":
            return "completed", info
        if st == "PENDING":
            return "payment_pending", info
        return flag(f"capture status {str(st)[:30]}")
    except (ValueError, TypeError, AttributeError, KeyError):
        return flag("unreadable PayPal answer")
