"""Every PayPal call the app makes logs PayPal's debug id at INFO with the purchase id and the PayPal-Request-Id, so a
sandbox or live problem can be traced with PayPal support. Never logged: the OAuth token, the Authorization header,
card numbers, vault token ids or merchant ids. Runs offline: httpx is replaced, nothing leaves the machine."""
from __future__ import annotations

import logging
from decimal import Decimal

import httpx
import pytest

from app.config import Settings
from app.paypal import PayPalClient, PayPalError

PID = "pu_0123456789ab"
MERCHANT = "SBXMERCHANT01"
VAULT = "vt-TOKEN-9z8y7x"
TOKEN = "A21AA-oauth-SECRET-token"
SECRETS = (TOKEN, "Bearer", "Authorization", MERCHANT, VAULT, "4111111111111111", "4032039317984658", "client-secret-x")


@pytest.fixture
def pp(monkeypatch):
    s = Settings(paypal_mode="sandbox", paypal_client_id="client-id-x", paypal_client_secret="client-secret-x",
                 paypal_base="https://api-m.sandbox.paypal.com")
    calls = []

    def post(url, **k):
        return httpx.Response(200, json={"access_token": TOKEN, "expires_in": 3600}, headers={"PayPal-Debug-Id": "dbgoauth01"})

    def request(method, url, json=None, headers=None, **k):
        calls.append((method, url, headers))
        dbg = f"dbg{len(calls):03d}"
        if url.endswith("/v3/vault/setup-tokens"):
            return httpx.Response(201, json={"id": "st-SETUP-1"}, headers={"PayPal-Debug-Id": dbg})
        if url.endswith("/v3/vault/payment-tokens"):
            return httpx.Response(201, json={"id": VAULT, "payment_source": {"card": {"brand": "VISA", "last_digits": "1111"}}},
                                  headers={"PayPal-Debug-Id": dbg})
        if url.endswith("/v2/checkout/orders") and json and json["purchase_units"][0].get("payee"):
            return httpx.Response(201, json={"id": "ORDER1", "status": "COMPLETED", "purchase_units": [
                {"payee": {"merchant_id": MERCHANT}, "custom_id": PID,
                 "payments": {"captures": [{"id": "CAP1", "status": "COMPLETED", "amount": {"value": "18.00"}}]}}]},
                headers={"PayPal-Debug-Id": dbg})
        if "/refund" in url:
            return httpx.Response(422, json={"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": "REFUND_FAILED"}]},
                                  headers={"PayPal-Debug-Id": dbg})
        if "/v2/payments/captures/" in url:
            return httpx.Response(200, json={"id": "CAP1", "status": "COMPLETED", "amount": {"value": "18.00", "currency_code": "USD"},
                                             "payee": {"merchant_id": MERCHANT}, "custom_id": PID},
                                  headers={"PayPal-Debug-Id": dbg})
        return httpx.Response(200, json={}, headers={"PayPal-Debug-Id": dbg})

    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(httpx, "request", request)
    c = PayPalClient(s)
    c.calls = calls
    return c


def _logs(caplog):
    return [r.getMessage() for r in caplog.records if r.name.startswith("agenttab")]


def _no_secrets(caplog):
    text = "\n".join(_logs(caplog)) + "\n".join(str(r.args) for r in caplog.records)
    for s in SECRETS:
        assert s not in text, s


def test_vendor_payment_logs_debug_id_purchase_and_request_id(pp, caplog):
    caplog.set_level(logging.INFO, logger="agenttab")
    pp.pay_vendor(VAULT, 1800, MERCHANT, PID, "Mug sample")
    line = next(m for m in _logs(caplog) if "/v2/checkout/orders" in m)
    assert "debug_id=dbg001" in line and f"purchase_id={PID}" in line and f"request_id=at-{PID}-pay" in line
    assert "status=201" in line
    assert any("debug_id=dbgoauth01" in m for m in _logs(caplog))      # the token call too, without the token
    _no_secrets(caplog)


def test_vault_calls_log_debug_ids_but_never_the_card_or_token(pp, caplog):
    caplog.set_level(logging.INFO, logger="agenttab")
    pp.vault_sandbox_card("demo-ag_1")
    logs = _logs(caplog)
    assert any("/v3/vault/setup-tokens" in m and "debug_id=dbg001" in m and "request_id=setup-demo-ag_1" in m for m in logs)
    assert any("/v3/vault/payment-tokens" in m and "debug_id=dbg002" in m for m in logs)
    _no_secrets(caplog)


def test_vault_token_id_in_a_path_is_redacted(pp, caplog):
    caplog.set_level(logging.INFO, logger="agenttab")
    pp.delete_vault_token(VAULT)
    assert any("/v3/vault/payment-tokens/<id>" in m and "debug_id=" in m for m in _logs(caplog))
    _no_secrets(caplog)


def test_get_capture_and_a_failed_refund_log_debug_ids(pp, caplog):
    caplog.set_level(logging.INFO, logger="agenttab")
    pp.get_capture("CAP1")
    with pytest.raises(PayPalError):
        pp.refund_capture("CAP1", Decimal("18.00"), op_id=f"purchase-{PID}")
    logs = _logs(caplog)
    assert any("GET /v2/payments/captures/CAP1" in m and "debug_id=dbg001" in m for m in logs)
    assert any("/refund" in m and "status=422" in m and "debug_id=dbg002" in m and f"purchase_id={PID}" in m for m in logs)
    _no_secrets(caplog)


def test_no_answer_is_logged_without_a_debug_id(pp, caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="agenttab")

    def boom(*a, **k):
        raise httpx.ReadTimeout("timed out")
    monkeypatch.setattr(httpx, "request", boom)
    with pytest.raises(PayPalError):
        pp.pay_vendor(VAULT, 1800, MERCHANT, PID, "Mug sample")
    assert any("no answer" in m and f"request_id=at-{PID}-pay" in m for m in _logs(caplog))
    _no_secrets(caplog)


def test_app_logs_reach_stderr_at_info(tmp_path):
    from app.main import create_app
    create_app(Settings(paypal_mode="mock", allow_insecure_mock=True, db_path=str(tmp_path / "x.db"), admin_token="adm",
                        llm_base_url="", llm_api_key="", anthropic_api_key=""))
    lg = logging.getLogger("agenttab")
    assert lg.getEffectiveLevel() <= logging.INFO and lg.handlers           # uvicorn only configures its own loggers
    create_app(Settings(paypal_mode="mock", allow_insecure_mock=True, db_path=str(tmp_path / "y.db"), admin_token="adm",
                        llm_base_url="", llm_api_key="", anthropic_api_key=""))
    assert len(lg.handlers) == 1                                             # no duplicate lines on a second app
