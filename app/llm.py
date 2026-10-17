"""Minimal OpenAI-compatible chat client (Gemini by default) with model fallback."""
from __future__ import annotations

import httpx

from .config import Settings


LAST_USAGE: dict = {}  # token usage of the most recent successful call (for the eval)


class LLMError(RuntimeError):
    pass


def enabled(s: Settings) -> bool:
    return bool(s.llm_base_url and s.llm_api_key)


def chat(s: Settings, messages: list[dict], json_mode: bool = False, temperature: float = 0.0,
         timeout: float = 25.0) -> tuple[str, str]:
    """Returns (text, model_used). Tries llm_model, then llm_fallback_model. Never includes the key in errors."""
    if not enabled(s):
        raise LLMError("LLM not configured")
    errors = []
    for model in dict.fromkeys(m for m in (s.llm_model, s.llm_fallback_model) if m):
        body = {"model": model, "messages": messages, "temperature": temperature}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        try:
            r = httpx.post(s.llm_base_url.rstrip("/") + "/chat/completions",
                           headers={"Authorization": f"Bearer {s.llm_api_key}"}, json=body, timeout=timeout)
        except httpx.HTTPError as e:
            errors.append(f"{model}: {type(e).__name__}")
            continue
        if r.status_code != 200:
            errors.append(f"{model}: HTTP {r.status_code}")
            continue
        try:
            j = r.json()
            text = j["choices"][0]["message"]["content"] or ""
            LAST_USAGE.clear()
            LAST_USAGE.update(j.get("usage") or {})
        except (ValueError, KeyError, IndexError):
            errors.append(f"{model}: bad response shape")
            continue
        return text, model
    raise LLMError("; ".join(errors))
