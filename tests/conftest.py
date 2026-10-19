"""Test isolation: no test sees the real AI, PayPal or vendor keys of the shell that runs pytest, and every app a test
builds starts with mock PayPal and the AI guard off. Tests marked `live` are the only exception; they run only with
AGENTTAB_LIVE_TESTS=1 (and their own LIVE_AI / LIVE_PAYPAL switches), and for them the keys are put back.

The keys are stripped once for the session (so module-scoped fixtures, such as the e2e uvicorn servers, start clean)
and again around every test with monkeypatch. Values are never printed."""
from __future__ import annotations

import os
import re

import pytest

PREFIXES = ("ANTHROPIC_", "GEMINI_", "LLM_")
NAMES = ("PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET", "VENDOR_PAYOUTS")
MERCHANT = re.compile(r"VENDOR_\w+_MERCHANT_ID")
LIVE_FLAG = "AGENTTAB_LIVE_TESTS"


def _secret(name: str) -> bool:
    return name.startswith(PREFIXES) or name in NAMES or bool(MERCHANT.fullmatch(name))


def _clear_settings_caches() -> None:
    """get_settings() builds a fresh Settings() today; clear it anyway if it ever gets an lru_cache."""
    from app import config
    for fn in (getattr(config, "get_settings", None),):
        if hasattr(fn, "cache_clear"):
            fn.cache_clear()


def pytest_configure(config):
    config.addinivalue_line("markers", f"live: calls a real outside service (AI or PayPal sandbox); runs only with {LIVE_FLAG}=1")


def pytest_collection_modifyitems(config, items):
    if os.getenv(LIVE_FLAG) == "1":
        return
    skip = pytest.mark.skip(reason=f"live test: set {LIVE_FLAG}=1 to run it")
    for it in items:
        if it.get_closest_marker("live"):
            it.add_marker(skip)


@pytest.fixture(scope="session", autouse=True)
def _isolated_env_session():
    saved = {k: os.environ[k] for k in list(os.environ) if _secret(k)}
    saved_mode = os.environ.get("PAYPAL_MODE")
    for k in saved:
        del os.environ[k]
    os.environ["PAYPAL_MODE"] = "mock"
    _clear_settings_caches()
    yield saved
    os.environ.update(saved)
    if saved_mode is None:
        os.environ.pop("PAYPAL_MODE", None)
    else:
        os.environ["PAYPAL_MODE"] = saved_mode
    _clear_settings_caches()


@pytest.fixture(autouse=True)
def _isolated_env(request, monkeypatch, _isolated_env_session):
    if request.node.get_closest_marker("live") and os.getenv(LIVE_FLAG) == "1":
        for k, v in _isolated_env_session.items():          # the live service needs its keys
            monkeypatch.setenv(k, v)
        _clear_settings_caches()
        yield
        _clear_settings_caches()
        return
    for k in [k for k in os.environ if _secret(k)]:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PAYPAL_MODE", "mock")
    _clear_settings_caches()
    yield
    _clear_settings_caches()
