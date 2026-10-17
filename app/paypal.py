"""PayPal REST client: Orders v2 (create / authorize / capture), Payments v2 (authorization capture / void,
refund, capture lookup) and Vault v3 (saved sandbox card for top-ups).

PAYPAL_MODE=sandbox talks to api-m.sandbox.paypal.com with client credentials.
PAYPAL_MODE=mock keeps the same shapes in memory so tests and offline demos run
without credentials.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from decimal import Decimal

import httpx

from .config import Settings


class PayPalError(RuntimeError):
    """issue = PayPal's error issue code (e.g. INSTRUMENT_DECLINED) when it sent one."""
    def __init__(self, msg: str, issue: str = "", status: int = 0):
        super().__init__(msg)
        self.issue, self.status = issue, status


# Forced declines (PayPal negative testing): only these codes, only in the sandbox or offline mock.
SIMULATABLE = {"INSTRUMENT_DECLINED", "TRANSACTION_REFUSED", "PAYER_CANNOT_PAY", "AUTHORIZATION_EXPIRED",
               "AUTHORIZATION_VOIDED", "REFUND_FAILED_INSUFFICIENT_FUNDS", "PAYMENT_DENIED"}


def _issue_of(text: str) -> str:
    try:
        j = json.loads(text)
    except ValueError:
        return ""
    d = j.get("details") or []
    return str((d[0].get("issue") if d and isinstance(d[0], dict) else "") or j.get("name") or "")


class PayPalClient:
    def __init__(self, settings: Settings):
        self.s = settings
        self._token: str | None = None
        self._token_exp = 0.0
        self._mock_orders: dict[str, dict] = {}
        self._mock_refunds: dict[str, dict] = {}
        self.mock_refund_log: list[tuple[str, Decimal]] = []   # offline mode: every refund actually issued
        self.mock_capture_count = 0
        self.mock_authorize_count = 0
        self._mock_auths: dict[str, dict] = {}
        self._mock_captures: dict[str, dict] = {}   # capture_id -> {amount, refunded, status}; tests may edit
        self._mock_vault: dict[str, dict] = {}
        self.mock_fail: dict[str, str] = {}          # offline tests: {"operation": "ISSUE"} fails it once

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

    def _req(self, method: str, path: str, json: dict | None = None, request_id: str = "",
             simulate: str = "") -> dict:
        # PayPal-Request-Id is derived from the logical operation (e.g. "capture-<order>"), so a retry of the
        # same operation is idempotent at PayPal instead of creating a second order/capture/refund.
        headers = {"Authorization": f"Bearer {self._access_token()}", "Content-Type": "application/json",
                   "PayPal-Request-Id": (request_id or f"agenttab-{uuid.uuid4()}")[:108],
                   "Prefer": "return=representation"}
        if simulate:
            headers["PayPal-Mock-Response"] = self._mock_header(simulate)
            headers["PayPal-Request-Id"] = f"sim-{uuid.uuid4().hex}"   # never replay a real result
        r = httpx.request(method, f"{self.s.paypal_base}{path}", json=json, headers=headers, timeout=30)
        if r.status_code >= 400:
            raise PayPalError(f"{method} {path}: {r.status_code} {r.text[:300]}", _issue_of(r.text), r.status_code)
        return {} if r.status_code == 204 else r.json()

    def _mock_header(self, code: str) -> str:
        """Negative-testing header. Refused outside the sandbox and for codes not on the allowlist."""
        if not self.is_sandbox:
            raise PayPalError("forced declines are only allowed in the PayPal sandbox")
        if code not in SIMULATABLE or not re.fullmatch(r"[A-Z_]{3,64}", code):
            raise PayPalError(f"cannot simulate {code!r}")
        return json.dumps({"mock_application_codes": code})

    def _mock_check(self, op: str, simulate: str = "") -> None:
        """Offline mode: fail like PayPal would (a test-injected failure, or a requested simulation)."""
        if simulate and simulate not in SIMULATABLE:
            raise PayPalError(f"cannot simulate {simulate!r}")
        code = simulate or self.mock_fail.pop(op, "")
        if code:
            raise PayPalError(f"{op}: 422 {code}", code, 422)

    def _mock_record_capture(self, cid: str, amount: Decimal) -> None:
        self._mock_captures.setdefault(cid, {"amount": Decimal(amount), "refunded": Decimal(0), "status": "COMPLETED"})

    @property
    def mock(self) -> bool:
        return self.s.paypal_mode != "sandbox"

    # ---------- orders ----------
    def create_order(self, amount: Decimal, agent_id: str, description: str, op_id: str = "",
                     intent: str = "CAPTURE") -> dict:
        """Returns {id, approve_url, status}. intent=AUTHORIZE only places a hold when the payer approves;
        nothing moves until capture_authorization() (and void_authorization() releases it)."""
        assert intent in ("CAPTURE", "AUTHORIZE")
        value = f"{amount:.2f}"
        if self.mock:
            oid = "MOCK-" + uuid.uuid4().hex[:12].upper()
            self._mock_orders[oid] = {"amount": value, "agent_id": agent_id, "status": "CREATED", "intent": intent}
            return {"id": oid, "status": "PAYER_ACTION_REQUIRED",
                    "approve_url": f"{self.s.public_url}/mock-paypal/approve/{oid}"}
        body = {
            "intent": intent,
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
        j = self._req("POST", "/v2/checkout/orders", body, request_id=f"order-{op_id or uuid.uuid4().hex}")
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
            if o["status"] == "COMPLETED":     # same PayPal-Request-Id: PayPal replays the first result
                return {"status": "COMPLETED", "amount": Decimal(o["amount"]),
                        "agent_id": o["agent_id"], "capture_id": "CAP-" + order_id[5:]}
            if o.get("intent") == "AUTHORIZE":
                raise PayPalError("ACTION_DOES_NOT_MATCH_INTENT", "ACTION_DOES_NOT_MATCH_INTENT", 422)
            if o["status"] != "APPROVED":
                raise PayPalError("ORDER_NOT_APPROVED")
            self._mock_check("capture_order")
            self.mock_capture_count += 1
            o["status"] = "COMPLETED"
            self._mock_record_capture("CAP-" + order_id[5:], Decimal(o["amount"]))
            return {"status": "COMPLETED", "amount": Decimal(o["amount"]),
                    "agent_id": o["agent_id"], "capture_id": "CAP-" + order_id[5:]}
        j = self._req("POST", f"/v2/checkout/orders/{order_id}/capture", {}, request_id=f"capture-{order_id}")
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

    def sandbox_card_topup(self, amount: Decimal, agent_id: str, op_id: str = "", simulate: str = "") -> dict:
        """Create + capture in one Orders v2 call with a sandbox test card (no browser approval).
        Refuses to run against live PayPal. Returns the same shape as capture_order()."""
        if self.mock:
            self._mock_check("card_topup", simulate)
            oid = "MOCK-" + uuid.uuid4().hex[:12].upper()
            self._mock_record_capture("CAP-" + oid[5:], amount)
            return {"order_id": oid, "status": "COMPLETED", "amount": amount, "agent_id": agent_id,
                    "capture_id": "CAP-" + oid[5:]}
        if not self.is_sandbox:
            raise PayPalError("test-card top-ups are only allowed in the PayPal sandbox")
        body = {"intent": "CAPTURE",
                "purchase_units": [{"reference_id": agent_id, "custom_id": agent_id,
                                    "description": f"AgentTab credit for {agent_id}"[:127],
                                    "amount": {"currency_code": "USD", "value": f"{amount:.2f}"}}],
                "payment_source": {"card": dict(self.SANDBOX_TEST_CARD)}}
        j = self._req("POST", "/v2/checkout/orders", body, request_id=f"card-{op_id or uuid.uuid4().hex}",
                      simulate=simulate)
        caps = j["purchase_units"][0].get("payments", {}).get("captures", [])
        if j.get("status") != "COMPLETED" or not caps or caps[0].get("status") != "COMPLETED":
            raise PayPalError(f"card capture not completed: {j.get('status')}")
        return {"order_id": j["id"], "status": "COMPLETED", "amount": Decimal(caps[0]["amount"]["value"]),
                "agent_id": agent_id, "capture_id": caps[0]["id"]}

    def refund_capture(self, capture_id: str, amount: Decimal, op_id: str = "", simulate: str = "") -> dict:
        """op_id names the logical refund (e.g. "purchase-<id>" or "tab-<capture>-<already refunded>"),
        so retrying the same refund never refunds twice."""
        rid = f"refund-{capture_id}-{op_id or uuid.uuid4().hex}"
        if self.mock:
            if rid in self._mock_refunds:
                return self._mock_refunds[rid]
            self._mock_check("refund", simulate)
            self.mock_refund_log.append((capture_id, amount))
            c = self._mock_captures.get(capture_id)
            if c:
                c["refunded"] += Decimal(amount)
                c["status"] = "REFUNDED" if c["refunded"] >= c["amount"] else "PARTIALLY_REFUNDED"
            self._mock_refunds[rid] = {"id": "REF-" + uuid.uuid4().hex[:10].upper(), "status": "COMPLETED"}
            return self._mock_refunds[rid]
        j = self._req("POST", f"/v2/payments/captures/{capture_id}/refund",
                      {"amount": {"currency_code": "USD", "value": f"{amount:.2f}"}}, request_id=rid, simulate=simulate)
        return {"id": j["id"], "status": j["status"]}

    # ---------- authorize, then capture or void (Payments v2) ----------
    def authorize_order(self, order_id: str) -> dict:
        """Payer approved an intent=AUTHORIZE order: place the hold. Returns {status, amount, agent_id,
        authorization_id}. Idempotent per order (PayPal-Request-Id authorize-<order>)."""
        if self.mock:
            o = self._mock_orders.get(order_id)
            if not o:
                raise PayPalError("unknown order")
            if o.get("intent") != "AUTHORIZE":
                raise PayPalError("ACTION_DOES_NOT_MATCH_INTENT", "ACTION_DOES_NOT_MATCH_INTENT", 422)
            aid = "AUTH-" + order_id[5:]
            if o["status"] == "COMPLETED":
                return {"status": "COMPLETED", "amount": Decimal(o["amount"]), "agent_id": o["agent_id"],
                        "authorization_id": aid}
            if o["status"] != "APPROVED":
                raise PayPalError("ORDER_NOT_APPROVED", "ORDER_NOT_APPROVED", 422)
            self._mock_check("authorize")
            self.mock_authorize_count += 1
            o["status"] = "COMPLETED"
            self._mock_auths[aid] = {"amount": Decimal(o["amount"]), "status": "CREATED"}
            return {"status": "COMPLETED", "amount": Decimal(o["amount"]), "agent_id": o["agent_id"],
                    "authorization_id": aid}
        j = self._req("POST", f"/v2/checkout/orders/{order_id}/authorize", {}, request_id=f"authorize-{order_id}")
        pu = j["purchase_units"][0]
        auth = pu["payments"]["authorizations"][0]
        if auth.get("status") not in ("CREATED", "CAPTURED"):
            raise PayPalError(f"authorization status {auth.get('status')}", str(auth.get("status")))
        return {"status": j["status"], "amount": Decimal(auth["amount"]["value"]),
                "agent_id": pu.get("reference_id") or auth.get("custom_id"), "authorization_id": auth["id"]}

    def card_authorize(self, amount: Decimal, agent_id: str, op_id: str, description: str = "") -> dict:
        """Sandbox test card, intent=AUTHORIZE: the hold is placed in one call. Returns {order_id,
        authorization_id, amount}. Refuses to run against live PayPal."""
        if self.mock:
            self._mock_check("card_authorize")
            oid = "MOCK-" + uuid.uuid4().hex[:12].upper()
            aid = "AUTH-" + oid[5:]
            self.mock_authorize_count += 1
            self._mock_auths[aid] = {"amount": Decimal(amount), "status": "CREATED"}
            return {"order_id": oid, "authorization_id": aid, "amount": Decimal(amount)}
        if not self.is_sandbox:
            raise PayPalError("test-card payments are only allowed in the PayPal sandbox")
        body = {"intent": "AUTHORIZE",
                "purchase_units": [{"reference_id": agent_id, "custom_id": agent_id,
                                    "description": (description or f"AgentTab purchase for {agent_id}")[:127],
                                    "amount": {"currency_code": "USD", "value": f"{amount:.2f}"}}],
                "payment_source": {"card": dict(self.SANDBOX_TEST_CARD)}}
        j = self._req("POST", "/v2/checkout/orders", body, request_id=f"cardauth-{op_id}")
        auths = j["purchase_units"][0].get("payments", {}).get("authorizations", [])
        if j.get("status") != "COMPLETED" or not auths or auths[0].get("status") != "CREATED":
            raise PayPalError(f"card authorization not created: {j.get('status')}")
        return {"order_id": j["id"], "authorization_id": auths[0]["id"], "amount": Decimal(auths[0]["amount"]["value"])}

    def capture_authorization(self, authorization_id: str, amount: Decimal, simulate: str = "") -> dict:
        """Take the money that was held. Returns {capture_id, status, amount}. Final capture: the rest of
        the hold (if any) is released. Idempotent per authorization."""
        if self.mock:
            a = self._mock_auths.get(authorization_id)
            if not a:
                raise PayPalError("unknown authorization", "INVALID_RESOURCE_ID", 404)
            cid = "CAP-" + authorization_id[5:]
            if a["status"] == "CAPTURED":
                return {"capture_id": cid, "status": "COMPLETED", "amount": a["amount"]}
            if a["status"] != "CREATED":
                raise PayPalError(f"AUTHORIZATION_{a['status']}", f"AUTHORIZATION_{a['status']}", 422)
            self._mock_check("capture_authorization", simulate)
            a["status"] = "CAPTURED"
            self._mock_record_capture(cid, a["amount"])
            return {"capture_id": cid, "status": "COMPLETED", "amount": a["amount"]}
        j = self._req("POST", f"/v2/payments/authorizations/{authorization_id}/capture",
                      {"amount": {"currency_code": "USD", "value": f"{amount:.2f}"}, "final_capture": True},
                      request_id=f"authcap-{authorization_id}", simulate=simulate)
        return {"capture_id": j["id"], "status": j["status"], "amount": Decimal(j["amount"]["value"])}

    def void_authorization(self, authorization_id: str) -> str:
        """Release the hold: the payer is never charged. Returns the final authorization status."""
        if self.mock:
            a = self._mock_auths.get(authorization_id)
            if not a:
                raise PayPalError("unknown authorization", "INVALID_RESOURCE_ID", 404)
            if a["status"] == "CAPTURED":
                raise PayPalError("AUTHORIZATION_ALREADY_CAPTURED", "AUTHORIZATION_ALREADY_CAPTURED", 422)
            self._mock_check("void")
            a["status"] = "VOIDED"
            return "VOIDED"
        j = self._req("POST", f"/v2/payments/authorizations/{authorization_id}/void", {},
                      request_id=f"void-{authorization_id}")
        return j.get("status", "VOIDED")

    def get_capture(self, capture_id: str) -> dict:
        """PayPal's view of one capture, for reconciliation: {status, amount}."""
        if self.mock:
            c = self._mock_captures.get(capture_id)
            if not c:
                raise PayPalError("unknown capture", "INVALID_RESOURCE_ID", 404)
            return {"status": c["status"], "amount": c["amount"]}
        j = self._req("GET", f"/v2/payments/captures/{capture_id}")
        return {"status": j["status"], "amount": Decimal(j["amount"]["value"])}

    # ---------- Vault v3: saved sandbox card for owner-approved top-ups ----------
    # Vault only accepts PayPal's generic sandbox test numbers; this is the standard test Visa. Fake money only.
    SANDBOX_VAULT_CARD = {"number": "4111111111111111", "expiry": "2030-12", "security_code": "123",
                          "name": "AgentTab Sandbox Tester"}

    def vault_sandbox_card(self, op_id: str) -> dict:
        """Save the sandbox test card: setup token -> payment token. Returns {vault_id, brand, last4}."""
        if self.mock:
            self._mock_check("vault")
            vid = "VAULT-" + uuid.uuid4().hex[:10].upper()
            self._mock_vault[vid] = {"brand": "VISA", "last4": "1111"}
            return {"vault_id": vid, "brand": "VISA", "last4": "1111"}
        if not self.is_sandbox:
            raise PayPalError("saving a test card is only allowed in the PayPal sandbox")
        st = self._req("POST", "/v3/vault/setup-tokens", {"payment_source": {"card": dict(self.SANDBOX_VAULT_CARD)}},
                       request_id=f"setup-{op_id}")
        pt = self._req("POST", "/v3/vault/payment-tokens",
                       {"payment_source": {"token": {"id": st["id"], "type": "SETUP_TOKEN"}}},
                       request_id=f"ptoken-{op_id}")
        card = (pt.get("payment_source") or {}).get("card") or {}
        return {"vault_id": pt["id"], "brand": card.get("brand", "CARD"), "last4": card.get("last_digits", "")}

    def charge_vault(self, vault_id: str, amount: Decimal, agent_id: str, op_id: str, simulate: str = "") -> dict:
        """Merchant-initiated charge of the saved card (stored credential, unscheduled). Same shape as
        sandbox_card_topup()."""
        if self.mock:
            if vault_id not in self._mock_vault:
                raise PayPalError("unknown vault token", "PERMISSION_DENIED", 403)
            self._mock_check("charge_vault", simulate)
            oid = "MOCK-" + uuid.uuid4().hex[:12].upper()
            self._mock_record_capture("CAP-" + oid[5:], amount)
            return {"order_id": oid, "status": "COMPLETED", "amount": Decimal(amount), "agent_id": agent_id,
                    "capture_id": "CAP-" + oid[5:]}
        if not self.is_sandbox:
            raise PayPalError("vaulted test-card charges are only allowed in the PayPal sandbox")
        body = {"intent": "CAPTURE",
                "purchase_units": [{"reference_id": agent_id, "custom_id": agent_id,
                                    "description": f"AgentTab saved-card top-up for {agent_id}"[:127],
                                    "amount": {"currency_code": "USD", "value": f"{amount:.2f}"}}],
                "payment_source": {"card": {"vault_id": vault_id, "stored_credential": {
                    "payment_initiator": "MERCHANT", "payment_type": "UNSCHEDULED", "usage": "SUBSEQUENT"}}}}
        j = self._req("POST", "/v2/checkout/orders", body, request_id=f"vault-{op_id}", simulate=simulate)
        caps = j["purchase_units"][0].get("payments", {}).get("captures", [])
        if j.get("status") != "COMPLETED" or not caps or caps[0].get("status") != "COMPLETED":
            raise PayPalError(f"saved-card capture not completed: {j.get('status')}")
        return {"order_id": j["id"], "status": "COMPLETED", "amount": Decimal(caps[0]["amount"]["value"]),
                "agent_id": agent_id, "capture_id": caps[0]["id"]}

    def delete_vault_token(self, vault_id: str) -> None:
        if self.mock:
            self._mock_vault.pop(vault_id, None)
            return
        self._req("DELETE", f"/v3/vault/payment-tokens/{vault_id}")

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
