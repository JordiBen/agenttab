"""PayPal REST client: Orders v2 (create / capture) and Payments v2 (refund).

PAYPAL_MODE=sandbox talks to api-m.sandbox.paypal.com with client credentials.
PAYPAL_MODE=mock keeps the same shapes in memory so tests and offline demos run
without credentials.
"""
from __future__ import annotations

import time
import uuid
from decimal import Decimal

import httpx

from .config import Settings


class PayPalError(RuntimeError):
    pass


class PayPalClient:
    def __init__(self, settings: Settings):
        self.s = settings
        self._token: str | None = None
        self._token_exp = 0.0
        self._mock_orders: dict[str, dict] = {}

    # ---------- auth ----------
    def _access_token(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        r = httpx.post(
            f"{self.s.paypal_base}/v1/oauth2/token",
            auth=(self.s.paypal_client_id, self.s.paypal_client_secret),
            data={"grant_type": "client_credentials"},
            timeout=20,
        )
        if r.status_code != 200:
            raise PayPalError(f"oauth failed: {r.status_code} {r.text[:200]}")
        j = r.json()
        self._token, self._token_exp = j["access_token"], time.time() + j.get("expires_in", 300)
        return self._token

    def _req(self, method: str, path: str, json: dict | None = None) -> dict:
        r = httpx.request(
            method,
            f"{self.s.paypal_base}{path}",
            json=json,
            headers={
                "Authorization": f"Bearer {self._access_token()}",
                "Content-Type": "application/json",
                "PayPal-Request-Id": str(uuid.uuid4()),  # idempotency
                "Prefer": "return=representation",
            },
            timeout=30,
        )
        if r.status_code >= 400:
            raise PayPalError(f"{method} {path}: {r.status_code} {r.text[:300]}")
        return r.json()

    @property
    def mock(self) -> bool:
        return self.s.paypal_mode != "sandbox"

    # ---------- orders ----------
    def create_order(self, amount: Decimal, agent_id: str, description: str) -> dict:
        """Returns {id, approve_url, status}."""
        value = f"{amount:.2f}"
        if self.mock:
            oid = "MOCK-" + uuid.uuid4().hex[:12].upper()
            self._mock_orders[oid] = {"amount": value, "agent_id": agent_id, "status": "CREATED"}
            return {"id": oid, "status": "PAYER_ACTION_REQUIRED",
                    "approve_url": f"{self.s.public_url}/mock-paypal/approve/{oid}"}
        body = {
            "intent": "CAPTURE",
            "purchase_units": [{
                "reference_id": agent_id,
                "custom_id": agent_id,
                "description": description[:127],
                "amount": {"currency_code": "USD", "value": value},
            }],
            "payment_source": {"paypal": {"experience_context": {
                "brand_name": "AgentTab by WebIntel Tools",
                "user_action": "PAY_NOW",
                "shipping_preference": "NO_SHIPPING",
                "return_url": f"{self.s.public_url}/paypal/return",
                "cancel_url": f"{self.s.public_url}/paypal/cancel",
            }}},
        }
        j = self._req("POST", "/v2/checkout/orders", body)
        link = next((l["href"] for l in j.get("links", []) if l["rel"] in ("payer-action", "approve")), None)
        return {"id": j["id"], "status": j["status"], "approve_url": link}

    def mock_approve(self, order_id: str) -> None:
        if order_id in self._mock_orders:
            self._mock_orders[order_id]["status"] = "APPROVED"

    def capture_order(self, order_id: str) -> dict:
        """Returns {status, amount (Decimal), agent_id, capture_id}."""
        if self.mock:
            o = self._mock_orders.get(order_id)
            if not o:
                raise PayPalError("unknown order")
            if o["status"] != "APPROVED":
                raise PayPalError("ORDER_NOT_APPROVED")
            o["status"] = "COMPLETED"
            return {"status": "COMPLETED", "amount": Decimal(o["amount"]),
                    "agent_id": o["agent_id"], "capture_id": "CAP-" + order_id[5:]}
        j = self._req("POST", f"/v2/checkout/orders/{order_id}/capture", {})
        pu = j["purchase_units"][0]
        cap = pu["payments"]["captures"][0]
        return {"status": j["status"], "amount": Decimal(cap["amount"]["value"]),
                "agent_id": pu.get("reference_id") or cap.get("custom_id"), "capture_id": cap["id"]}

    # PayPal's public sandbox test Visa (developer.paypal.com/tools/sandbox/card-testing). Fake money only.
    SANDBOX_TEST_CARD = {"number": "4032039317984658", "expiry": "2030-12", "security_code": "123",
                         "name": "AgentTab Sandbox Tester"}

    @property
    def is_sandbox(self) -> bool:
        return self.s.paypal_mode == "sandbox" and "sandbox.paypal.com" in self.s.paypal_base

    def sandbox_card_topup(self, amount: Decimal, agent_id: str) -> dict:
        """Create + capture in one Orders v2 call with a sandbox test card (no browser approval).
        Refuses to run against live PayPal. Returns the same shape as capture_order()."""
        if self.mock:
            oid = "MOCK-" + uuid.uuid4().hex[:12].upper()
            return {"order_id": oid, "status": "COMPLETED", "amount": amount, "agent_id": agent_id,
                    "capture_id": "CAP-" + oid[5:]}
        if not self.is_sandbox:
            raise PayPalError("test-card top-ups are only allowed in the PayPal sandbox")
        body = {"intent": "CAPTURE",
                "purchase_units": [{"reference_id": agent_id, "custom_id": agent_id,
                                    "description": f"AgentTab credit for {agent_id}"[:127],
                                    "amount": {"currency_code": "USD", "value": f"{amount:.2f}"}}],
                "payment_source": {"card": dict(self.SANDBOX_TEST_CARD)}}
        j = self._req("POST", "/v2/checkout/orders", body)
        caps = j["purchase_units"][0].get("payments", {}).get("captures", [])
        if j.get("status") != "COMPLETED" or not caps or caps[0].get("status") != "COMPLETED":
            raise PayPalError(f"card capture not completed: {j.get('status')}")
        return {"order_id": j["id"], "status": "COMPLETED", "amount": Decimal(caps[0]["amount"]["value"]),
                "agent_id": agent_id, "capture_id": caps[0]["id"]}

    def refund_capture(self, capture_id: str, amount: Decimal) -> dict:
        if self.mock:
            return {"id": "REF-" + uuid.uuid4().hex[:10].upper(), "status": "COMPLETED"}
        j = self._req("POST", f"/v2/payments/captures/{capture_id}/refund",
                      {"amount": {"currency_code": "USD", "value": f"{amount:.2f}"}})
        return {"id": j["id"], "status": j["status"]}

    # ---------- webhooks ----------
    WEBHOOK_EVENTS = ["CHECKOUT.ORDER.APPROVED", "PAYMENT.CAPTURE.COMPLETED", "PAYMENT.CAPTURE.REFUNDED",
                      "PAYMENT.CAPTURE.DENIED", "PAYMENT.CAPTURE.REVERSED", "PAYMENT.CAPTURE.PENDING"]

    def verify_webhook(self, headers: dict, event: dict, webhook_id: str) -> bool:
        """PayPal's verify-webhook-signature API. Header names are case-insensitive."""
        h = {k.lower(): v for k, v in headers.items()}
        need = ["paypal-auth-algo", "paypal-cert-url", "paypal-transmission-id", "paypal-transmission-sig",
                "paypal-transmission-time"]
        if not webhook_id or any(not h.get(k) for k in need):
            return False
        j = self._req("POST", "/v1/notifications/verify-webhook-signature", {
            "auth_algo": h["paypal-auth-algo"], "cert_url": h["paypal-cert-url"],
            "transmission_id": h["paypal-transmission-id"], "transmission_sig": h["paypal-transmission-sig"],
            "transmission_time": h["paypal-transmission-time"], "webhook_id": webhook_id, "webhook_event": event})
        return j.get("verification_status") == "SUCCESS"

    def list_webhooks(self) -> list[dict]:
        return self._req("GET", "/v1/notifications/webhooks").get("webhooks", [])

    def create_webhook(self, url: str) -> dict:
        return self._req("POST", "/v1/notifications/webhooks",
                         {"url": url, "event_types": [{"name": n} for n in self.WEBHOOK_EVENTS]})
