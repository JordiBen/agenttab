"""Runtime configuration (all from environment variables).

Every default is read when a Settings() object is created (default_factory), not when this module is
imported, so tests and app factories see the environment as it is at that moment."""
import os
from dataclasses import dataclass, field


GEMINI_OPENAI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"


def _env(name: str, default: str = ""):
    return field(default_factory=lambda: os.getenv(name, default))


def _env_int(name: str, default: int):
    return field(default_factory=lambda: int(os.getenv(name, str(default))))


def _llm_key() -> str:
    return os.getenv("LLM_API_KEY") or os.getenv("GEMINI_API_KEY", "")


@dataclass(frozen=True)
class Settings:
    paypal_mode: str = _env("PAYPAL_MODE", "mock")  # mock | sandbox
    paypal_client_id: str = _env("PAYPAL_CLIENT_ID")
    paypal_client_secret: str = _env("PAYPAL_CLIENT_SECRET")
    paypal_base: str = _env("PAYPAL_BASE", "https://api-m.sandbox.paypal.com")
    public_url: str = _env("PUBLIC_URL", "http://localhost:8000")
    paypal_webhook_id: str = _env("PAYPAL_WEBHOOK_ID")   # from developer dashboard or scripts/register_webhook.py
    db_path: str = _env("AGENTTAB_DB", "agenttab.sqlite3")
    # Any OpenAI-compatible chat endpoint: Gemini, Groq, OpenRouter, Ollama...
    # Defaults to Google Gemini (free tier) when GEMINI_API_KEY is set.
    llm_api_key: str = field(default_factory=_llm_key)
    llm_base_url: str = field(default_factory=lambda: os.getenv("LLM_BASE_URL") or (GEMINI_OPENAI_BASE if _llm_key() else ""))
    # Guard = short classification, so a fast Flash-Lite first; newest Flash as second try.
    llm_model: str = _env("LLM_MODEL", "gemini-3.5-flash-lite")
    llm_fallback_model: str = _env("LLM_FALLBACK_MODEL", "gemini-3.8-flash")
    # Spend-guard AI provider: "anthropic" (default; official SDK, ANTHROPIC_API_KEY) or "gemini" (the
    # OpenAI-compatible client above). Reports keep using the OpenAI-compatible client when one is configured.
    guard_provider: str = _env("GUARD_PROVIDER", "anthropic")
    anthropic_api_key: str = _env("ANTHROPIC_API_KEY")
    anthropic_model: str = _env("ANTHROPIC_MODEL", "claude-haiku-5-5")
    # Owner/admin token. Must come from the environment; there is no default value.
    # If unset, create_app() generates a random one and writes it to ./.admin_token (chmod 600).
    admin_token: str = _env("ADMIN_TOKEN")
    # Public demo: anyone can open a sandboxed demo session; owner endpoints stay admin-only.
    demo_mode: bool = field(default_factory=lambda: os.getenv("DEMO_MODE", "0") == "1")
    # Vendor payouts, capture-only (docs/vendor-payouts-impl-plan.md). Off unless exactly "1".
    vendor_payouts: bool = field(default_factory=lambda: os.getenv("VENDOR_PAYOUTS", "0") == "1")
    demo_sessions_per_ip_hour: int = _env_int("DEMO_SESSIONS_PER_IP_HOUR", 3)
    demo_sessions_per_day: int = _env_int("DEMO_SESSIONS_PER_DAY", 40)
    tool_calls_per_ip_hour: int = _env_int("TOOL_CALLS_PER_IP_HOUR", 40)
    purchases_per_agent_hour: int = _env_int("PURCHASES_PER_AGENT_HOUR", 30)
    topup_requests_per_agent_hour: int = _env_int("TOPUP_REQUESTS_PER_AGENT_HOUR", 5)
    reports_per_agent_hour: int = _env_int("REPORTS_PER_AGENT_HOUR", 5)
    tab_refunds_per_agent_hour: int = _env_int("TAB_REFUNDS_PER_AGENT_HOUR", 3)
    # Separate daily LLM budgets: spend-guard checks can't be starved by report requests.
    llm_calls_per_day: int = _env_int("LLM_CALLS_PER_DAY", 400)            # guard; then purchases need approval
    llm_report_calls_per_day: int = _env_int("LLM_REPORT_CALLS_PER_DAY", 60)  # reports; then a template
    # Per visitor IP, across guard checks and the Gauntlet: one visitor can't spend the shared AI quota.
    llm_calls_per_ip_day: int = _env_int("LLM_CALLS_PER_IP_DAY", 60)
    verdict_cache_ttl_s: float = field(default_factory=lambda: float(os.getenv("VERDICT_CACHE_TTL_S", "600")))
    paypal_calls_per_day: int = _env_int("PAYPAL_CALLS_PER_DAY", 120)  # orders/captures/refunds
    # A purchase waits at most this long for the same agent's previous purchase, then gets 429 (busy).
    agent_lock_timeout_s: float = field(default_factory=lambda: float(os.getenv("AGENT_LOCK_TIMEOUT_S", "2.5")))
    # Live Gauntlet: a sampled attack run through the real guard (dry run, no money). Own LLM budget.
    gauntlet_runs_per_ip_hour: int = _env_int("GAUNTLET_RUNS_PER_IP_HOUR", 2)
    gauntlet_runs_per_day: int = _env_int("GAUNTLET_RUNS_PER_DAY", 12)
    gauntlet_llm_calls_per_day: int = _env_int("GAUNTLET_LLM_CALLS_PER_DAY", 60)
    # Compare panel ("Rules + keyword check" vs "Rules + AI" on one purchase): dry run, its own LLM budget,
    # not shared with the Gauntlet; its calls also count against llm_calls_per_ip_day.
    compare_runs_per_ip_hour: int = _env_int("COMPARE_RUNS_PER_IP_HOUR", 5)
    compare_runs_per_day: int = _env_int("COMPARE_RUNS_PER_DAY", 60)
    compare_llm_calls_per_day: int = _env_int("COMPARE_LLM_CALLS_PER_DAY", 40)
    # Offline mock PayPal is for localhost only. Tests set this to run mock + demo mode together.
    allow_insecure_mock: bool = field(default_factory=lambda: os.getenv("ALLOW_INSECURE_MOCK", "0") == "1")


def get_settings() -> Settings:
    return Settings()
