"""Runtime configuration (all from environment variables)."""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    paypal_mode: str = os.getenv("PAYPAL_MODE", "mock")  # mock | sandbox
    paypal_client_id: str = os.getenv("PAYPAL_CLIENT_ID", "")
    paypal_client_secret: str = os.getenv("PAYPAL_CLIENT_SECRET", "")
    paypal_base: str = os.getenv("PAYPAL_BASE", "https://api-m.sandbox.paypal.com")
    public_url: str = os.getenv("PUBLIC_URL", "http://localhost:8000")
    db_path: str = os.getenv("AGENTTAB_DB", "agenttab.sqlite3")
    # Any OpenAI-compatible chat endpoint: Gemini, Groq, OpenRouter, Ollama...
    llm_base_url: str = os.getenv("LLM_BASE_URL", "")
    llm_api_key: str = os.getenv("LLM_API_KEY", "")
    llm_model: str = os.getenv("LLM_MODEL", "gemini-2.5-flash")
    admin_token: str = os.getenv("ADMIN_TOKEN", "dev-admin")


def get_settings() -> Settings:
    return Settings()
