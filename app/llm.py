"""LLM clients: a minimal OpenAI-compatible chat client (Gemini, with model fallback) and the Anthropic spend-guard call."""
from __future__ import annotations

import json

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


# ---------------- spend-guard provider (Anthropic by default, Gemini optional) ----------------
VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "target_allowed": {"type": "boolean"},
        "purpose_fits_policy": {"type": "boolean"},
        "injection_attempt": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["target_allowed", "purpose_fits_policy", "injection_attempt"],
    "additionalProperties": False,
}


def guard_enabled(s: Settings) -> bool:
    """Is an AI provider configured for the spend guard?"""
    if s.guard_provider == "anthropic":
        return bool(s.anthropic_api_key)
    if s.guard_provider == "gemini":
        return enabled(s)
    return False


def guard_model(s: Settings) -> str:
    """The model id that judges purchases (part of the verdict-cache key)."""
    return s.anthropic_model if s.guard_provider == "anthropic" else s.llm_model


def guard_models(s: Settings) -> list[str]:
    if not guard_enabled(s):
        return []
    if s.guard_provider == "anthropic":
        return [s.anthropic_model]
    return [m for m in dict.fromkeys((s.llm_model, s.llm_fallback_model)) if m]


_CLIENTS: dict = {}


def _anthropic_client(s: Settings):
    import anthropic
    c = _CLIENTS.get(s.anthropic_api_key)
    if c is None:
        c = _CLIENTS[s.anthropic_api_key] = anthropic.Anthropic(api_key=s.anthropic_api_key, timeout=8.0, max_retries=1)
    return c


def anthropic_verdict(s: Settings, system: str, user_json: str) -> tuple[dict | None, str, str]:
    """One structured-output call to Claude. Returns (verdict, model, problem); problem == "" means a complete answer.
    Raises LLMError when the model can't be asked (rate limit, API status error, connection): the caller treats that
    as "unavailable" and sends the purchase to a human. No temperature and no thinking parameter are sent."""
    import anthropic
    model = s.anthropic_model
    try:
        msg = _anthropic_client(s).messages.create(
            model=model, max_tokens=2000, system=system,
            messages=[{"role": "user", "content": user_json}],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": VERDICT_SCHEMA}})
    except anthropic.RateLimitError:
        raise LLMError(f"{model}: rate limited (429)") from None
    except anthropic.APIStatusError as e:
        raise LLMError(f"{model}: HTTP {e.status_code}") from None
    except anthropic.APIConnectionError as e:
        raise LLMError(f"{model}: {type(e).__name__}") from None
    u = getattr(msg, "usage", None)
    LAST_USAGE.clear()
    if u is not None:
        LAST_USAGE.update({"total_tokens": (u.input_tokens or 0) + (u.output_tokens or 0)})
    if msg.stop_reason != "end_turn":
        return None, model, f"stop_reason={msg.stop_reason}"
    text = next((b.text for b in msg.content if getattr(b, "type", "") == "text"), None)
    if text is None:
        return None, model, "no text block"
    try:
        j = json.loads(text)
    except ValueError:
        return None, model, "text block is not JSON"
    return (j if isinstance(j, dict) else None), model, ("" if isinstance(j, dict) else "verdict is not an object")
