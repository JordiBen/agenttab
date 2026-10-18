"""PayPal REST client: Orders v2 (create / authorize / capture), Payments v2 (authorization capture / void,
refund, capture lookup) and Vault v3 (saved sandbox card for top-ups).

PAYPAL_MODE=sandbox talks to api-m.sandbox.paypal.com with client credentials.
PAYPAL_MODE=mock keeps the same shapes in memory so tests and offline demos run
without credentials.
"""
from __future__ import annotations

import copy
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
        # Vendor payouts (offline): every pay_vendor request (body + PayPal-Request-Id), the orders "PayPal" made
        # (keyed by Request-Id, so a repeat replays the first result), a per-call script and a response tamper hook.
        self.mock_requests: list[dict] = []
        self.mock_vendor_orders: dict[str, dict] = {}
        self.mock_vendor_script: list = []   # "ok" | "timeout_before" | "timeout_after" | ("error", http, issue)
        self.mock_vendor_tamper = None       # callable(response) -> None, edits the returned copy
        self.mock_token_resets = 0

    def reset_token(self) -> None:
        """Forget the OAuth token (PayPal said 401), so the next request fetches a fresh one."""
        self._token, self._token_exp = None, 0.0
        self.mock_token_resets += 1

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

    def _mock_record_capture(self, cid: str, amount: Decimal, payee: str | None = None, **extra) -> None:
        self._mock_captures.setdefault(cid, {"amount": Decimal(amount), "refunded": Decimal(0), "status": "COMPLETED",
                                             **({"payee": payee} if payee else {}), **extra})

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
                "brand_name": "AgentTab",
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
        """PayPal's view of one capture, for reconciliation: {status, amount, currency, payee (merchant id or None),
        custom_id, order_id}."""
        if self.mock:
            code = self.mock_fail.pop("get_capture", "")
            if code:                              # offline tests: PayPal unavailable for this lookup
                raise PayPalError(f"get_capture: 503 {code}", code, 503)
            c = self._mock_captures.get(capture_id)
            if not c:
                raise PayPalError("unknown capture", "INVALID_RESOURCE_ID", 404)
            return {"status": c["status"], "amount": c["amount"], "currency": c.get("currency", "USD"),
                    "payee": c.get("payee"), "custom_id": c.get("custom_id"), "order_id": c.get("order_id")}
        if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", capture_id or ""):
            raise PayPalError("bad capture id", "INVALID_RESOURCE_ID", 404)
        try:
            j = self._req("GET", f"/v2/payments/captures/{capture_id}")
        except httpx.HTTPError as e:
            raise PayPalError(f"GET capture: {type(e).__name__}", "NETWORK", 0)
        return {"status": j["status"], "amount": Decimal(j["amount"]["value"]),
                "currency": (j.get("amount") or {}).get("currency_code"),
                "payee": (j.get("payee") or {}).get("merchant_id"), "custom_id": j.get("custom_id"),
                "order_id": ((j.get("supplementary_data") or {}).get("related_ids") or {}).get("order_id")}

    def get_order(self, order_id: str) -> dict:
        """PayPal's order JSON (GET /v2/checkout/orders/{id}), unchanged; vendor payouts check it like a create answer."""
        if self.mock:
            for o in self.mock_vendor_orders.values():
                if o["id"] == order_id:
                    return copy.deepcopy(o)
            raise PayPalError("unknown order", "INVALID_RESOURCE_ID", 404)
        if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", order_id or ""):
            raise PayPalError("bad order id", "INVALID_RESOURCE_ID", 404)
        try:
            return self._req("GET", f"/v2/checkout/orders/{order_id}")
        except httpx.HTTPError as e:
            raise PayPalError(f"GET order: {type(e).__name__}", "NETWORK", 0)

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

    def authorize_vault(self, vault_id: str, amount: Decimal, agent_id: str, op_id: str, simulate: str = "") -> dict:
        """Merchant-initiated HOLD on the saved card (intent=AUTHORIZE, stored credential, unscheduled). Nothing
        moves until capture_authorization(); void_authorization() releases it. Returns {order_id,
        authorization_id, amount}. Used by auto top-up so the card is only charged after the purchase commits."""
        if self.mock:
            if vault_id not in self._mock_vault:
                raise PayPalError("unknown vault token", "PERMISSION_DENIED", 403)
            self._mock_check("authorize_vault", simulate)
            oid = "MOCK-" + uuid.uuid4().hex[:12].upper()
            aid = "AUTH-" + oid[5:]
            self.mock_authorize_count += 1
            self._mock_auths[aid] = {"amount": Decimal(amount), "status": "CREATED"}
            return {"order_id": oid, "authorization_id": aid, "amount": Decimal(amount)}
        if not self.is_sandbox:
            raise PayPalError("vaulted test-card charges are only allowed in the PayPal sandbox")
        body = {"intent": "AUTHORIZE",
                "purchase_units": [{"reference_id": agent_id, "custom_id": agent_id,
                                    "description": f"AgentTab automatic top-up for {agent_id}"[:127],
                                    "amount": {"currency_code": "USD", "value": f"{amount:.2f}"}}],
                "payment_source": {"card": {"vault_id": vault_id, "stored_credential": {
                    "payment_initiator": "MERCHANT", "payment_type": "UNSCHEDULED", "usage": "SUBSEQUENT"}}}}
        j = self._req("POST", "/v2/checkout/orders", body, request_id=f"vaultauth-{op_id}", simulate=simulate)
        auths = j["purchase_units"][0].get("payments", {}).get("authorizations", [])
        if j.get("status") != "COMPLETED" or not auths or auths[0].get("status") != "CREATED":
            raise PayPalError(f"saved-card authorization not created: {j.get('status')}")
        return {"order_id": j["id"], "authorization_id": auths[0]["id"], "amount": Decimal(auths[0]["amount"]["value"])}

    # ---------- vendor payouts, capture-only (docs/vendor-payouts-impl-plan.md) ----------
    @staticmethod
    def vendor_order_body(vault_id: str, amount_cents: int, payee_merchant_id: str, pid: str, description: str) -> dict:
        """The body that completed in the capture-only spike (debug id f17615595ff9b): one CAPTURE order, the
        vendor as payee, the owner's saved card as a merchant-initiated stored credential. Amount from cents."""
        from .ledger import cents_str
        return {"intent": "CAPTURE",
                "purchase_units": [{"reference_id": pid, "custom_id": pid, "description": description[:127],
                                    "amount": {"currency_code": "USD", "value": cents_str(amount_cents)},
                                    "payee": {"merchant_id": payee_merchant_id}}],
                "payment_source": {"card": {"vault_id": vault_id, "stored_credential": {
                    "payment_initiator": "MERCHANT", "payment_type": "UNSCHEDULED", "usage": "SUBSEQUENT"}}}}

    def pay_vendor(self, vault_id: str, amount_cents: int, payee_merchant_id: str, pid: str, description: str) -> dict:
        """Pay the vendor straight from the owner's saved card: POST /v2/checkout/orders, PayPal-Request-Id
        at-{pid}-pay (a repeat returns the first request's result). Returns PayPal's order JSON unchanged; the
        caller checks status, payee and amount. No answer (timeout, network) raises PayPalError with status 0.
        A 401 means our OAuth token expired (PayPal did nothing): fetch a new token and retry once with the same
        Request-Id. A second 401 is raised with auth_retried=True (vendorpay.definite_refusal counts only that)."""
        try:
            return self._pay_vendor_once(vault_id, amount_cents, payee_merchant_id, pid, description)
        except PayPalError as e:
            if e.status != 401:
                raise
        self.reset_token()
        try:
            return self._pay_vendor_once(vault_id, amount_cents, payee_merchant_id, pid, description)
        except PayPalError as e:
            if e.status == 401:
                e.auth_retried = True
            raise

    def _pay_vendor_once(self, vault_id: str, amount_cents: int, payee_merchant_id: str, pid: str,
                         description: str) -> dict:
        from .vendorpay import request_id
        rid = request_id(pid, "pay")
        body = self.vendor_order_body(vault_id, amount_cents, payee_merchant_id, pid, description)
        if self.mock:
            self.mock_requests.append({"op": "pay_vendor", "request_id": rid, "body": copy.deepcopy(body)})
            step = self.mock_vendor_script.pop(0) if self.mock_vendor_script else "ok"
            if step == "timeout_before":
                raise PayPalError("POST /v2/checkout/orders: timeout", "TIMEOUT", 0)
            if isinstance(step, tuple) and step[0] == "error":
                raise PayPalError(f"POST /v2/checkout/orders: {step[1]} {step[2]}", step[2], step[1])
            if rid not in self.mock_vendor_orders:
                if vault_id not in self._mock_vault:
                    raise PayPalError("unknown vault token", "PERMISSION_DENIED", 403)
                oid = "MOCK-" + uuid.uuid4().hex[:12].upper()
                cid = "CAP-" + oid[5:]
                u = copy.deepcopy(body["purchase_units"][0])
                u.pop("description", None)
                u["payments"] = {"captures": [{"id": cid, "status": "COMPLETED", "amount": dict(u["amount"]),
                                               "final_capture": True, "custom_id": pid}]}
                self.mock_vendor_orders[rid] = {"id": oid, "status": "COMPLETED", "purchase_units": [u]}
                self._mock_record_capture(cid, Decimal(u["amount"]["value"]), payee_merchant_id, custom_id=pid, order_id=oid)
            if step == "timeout_after":          # PayPal made the order, the answer was lost
                raise PayPalError("POST /v2/checkout/orders: timeout", "TIMEOUT", 0)
            out = copy.deepcopy(self.mock_vendor_orders[rid])
            if self.mock_vendor_tamper:
                self.mock_vendor_tamper(out)
            return out
        if not self.is_sandbox:
            raise PayPalError("vendor payments are only allowed in the PayPal sandbox", "SANDBOX_ONLY", 403)
        try:
            return self._req("POST", "/v2/checkout/orders", body, request_id=rid)
        except httpx.HTTPError as e:
            raise PayPalError(f"POST /v2/checkout/orders: {type(e).__name__}",
                              "TIMEOUT" if isinstance(e, httpx.TimeoutException) else "NETWORK", 0)

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
