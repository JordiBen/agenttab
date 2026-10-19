"""Test isolation (reviewer t77u): tests never see the real AI, PayPal or vendor keys of the shell that runs them, and
the app they build runs mock PayPal with the AI guard off. tests/conftest.py strips the keys and sets PAYPAL_MODE=mock;
tests marked `live` are the only exception and run only with AGENTTAB_LIVE_TESTS=1."""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
SECRET_PREFIXES = ("ANTHROPIC_", "GEMINI_", "LLM_")
SECRET_NAMES = ("PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET", "VENDOR_PAYOUTS")
FAKE = "sk-ant-fake-isolation-probe-not-a-key"
LIVE_TESTS = ["tests/test_agenttab.py::test_live_gemini_guard", "tests/test_agenttab.py::test_live_paypal_sandbox",
              "tests/test_claude_guard.py::test_live_claude_allows_a_legit_research_buy",
              "tests/test_claude_guard.py::test_live_claude_blocks_a_forbidden_target",
              "tests/test_claude_guard.py::test_live_claude_blocks_an_off_policy_purpose",
              "tests/test_paypal_depth.py::test_live_sandbox_authorize_capture_void_declines_and_vault"]


def _leaks() -> list[str]:
    return sorted(k for k in os.environ if k.startswith(SECRET_PREFIXES) or k in SECRET_NAMES
                  or re.fullmatch(r"VENDOR_\w+_MERCHANT_ID", k))


def test_probe_no_keys_in_env_and_the_app_starts_in_mock_mode(tmp_path, monkeypatch):
    assert _leaks() == []                                          # names only; values are never printed
    assert os.environ.get("PAYPAL_MODE") == "mock"
    from app.config import get_settings
    from app.main import create_app
    s = get_settings()
    assert s.paypal_mode == "mock" and not s.anthropic_api_key and not s.llm_api_key and not s.paypal_client_id
    assert not s.paypal_client_secret and not s.vendor_payouts
    monkeypatch.setenv("AGENTTAB_DB", str(tmp_path / "iso.db"))
    monkeypatch.setenv("ADMIN_TOKEN", "isolation-probe-admin-token-x")
    h = TestClient(create_app(get_settings())).get("/healthz").json()
    assert h["paypal_mode"] == "mock" and h["ai_guard"] is False and h["ai_status"] == "off", h
    assert "vendors_payable" not in h


@pytest.mark.skipif(os.getenv("AGENTTAB_ISOLATION_CHILD") == "1", reason="inside the child run")
def test_a_fake_key_in_the_process_env_reaches_no_test():
    """Run the probe in a child pytest whose environment carries fake keys (fake values, never real ones)."""
    env = dict(os.environ, AGENTTAB_ISOLATION_CHILD="1", ANTHROPIC_API_KEY=FAKE, GEMINI_API_KEY=FAKE, LLM_API_KEY=FAKE,
               PAYPAL_CLIENT_ID="fake-id", PAYPAL_CLIENT_SECRET="fake-secret", PAYPAL_MODE="sandbox",
               VENDOR_PAYOUTS="1", VENDOR_SHOPSUITE_MERCHANT_ID="FAKEMERCHANT1")
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                        "tests/test_env_isolation.py::test_probe_no_keys_in_env_and_the_app_starts_in_mock_mode"],
                       cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert r.returncode == 0 and "1 passed" in r.stdout, r.stdout[-2000:]
    assert FAKE not in r.stdout


def test_live_marker_is_registered_and_on_every_live_test():
    r = subprocess.run([sys.executable, "-m", "pytest", "--markers"], cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    assert "@pytest.mark.live" in r.stdout
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "--collect-only", "-m", "live", "-p", "no:cacheprovider"],
                       cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=120)
    got = sorted(l.strip() for l in r.stdout.splitlines() if "::test_" in l)
    assert got == sorted(LIVE_TESTS), got


@pytest.mark.skipif(os.getenv("AGENTTAB_ISOLATION_CHILD") == "1", reason="inside the child run")
def test_live_tests_skip_without_the_explicit_flag():
    env = {k: v for k, v in os.environ.items() if k != "AGENTTAB_LIVE_TESTS"} | {"LIVE_AI": "1", "LIVE_PAYPAL": "1",
                                                                              "AGENTTAB_ISOLATION_CHILD": "1"}
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-rs", "-m", "live", "-p", "no:cacheprovider"], cwd=ROOT,
                       env=env, capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert "6 skipped" in r.stdout and "AGENTTAB_LIVE_TESTS=1" in r.stdout, r.stdout[-2000:]
    assert " passed" not in r.stdout and " failed" not in r.stdout
