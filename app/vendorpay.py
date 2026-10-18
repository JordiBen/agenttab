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
