"""Runtime configuration (all from environment variables)."""
import os
from dataclasses import dataclass


GEMINI_OPENAI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"


@dataclass(frozen=True)
class Settings:
    paypal_mode: str = os.getenv("PAYPAL_MODE", "mock")  # mock | sandbox
    paypal_client_id: str = os.getenv("PAYPAL_CLIENT_ID", "")
    paypal_client_secret: str = os.getenv("PAYPAL_CLIENT_SECRET", "")
    paypal_base: str = os.getenv("PAYPAL_BASE", "https://api-m.sandbox.paypal.com")
    public_url: str = os.getenv("PUBLIC_URL", "http://localhost:8000")
    db_path: str = os.getenv("AGENTTAB_DB", "agenttab.sqlite3")
    # Any OpenAI-compatible chat endpoint: Gemini, Groq, OpenRouter, Ollama...
    # Defaults to Google Gemini (free tier) when GEMINI_API_KEY is set.
    llm_api_key: str = os.getenv("LLM_API_KEY") or os.getenv("GEMINI_API_KEY", "")
    llm_base_url: str = os.getenv("LLM_BASE_URL") or (GEMINI_OPENAI_BASE if (os.getenv("LLM_API_KEY") or os.getenv("GEMINI_API_KEY")) else "")
    # Guard = short classification, so a fast Flash-Lite first; newest Flash as second try.
    llm_model: str = os.getenv("LLM_MODEL", "gemini-3.5-flash-lite")
    llm_fallback_model: str = os.getenv("LLM_FALLBACK_MODEL", "gemini-3.8-flash")
    admin_token: str = os.getenv("ADMIN_TOKEN", "dev-admin")


def get_settings() -> Settings:
    return Settings()
