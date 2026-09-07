"""Shared LLM-provider config: pick the backend via env, use everywhere.

    LLM_PROVIDER=gemini   -> Google Gemini (default; needs GEMINI_API_KEY)
    LLM_PROVIDER=nvidia   -> NVIDIA NIM (OpenAI-compatible; needs NVIDIA_API_KEY)

Both the parser (structured extraction) and the browser agent read from here,
so switching providers is a one-line .env change.
"""
from __future__ import annotations

import os

GEMINI_DEFAULT_MODEL = "gemini-3.6-flash"
NVIDIA_DEFAULT_MODEL = "meta/llama-3.3-70b-instruct"
NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"


def provider() -> str:
    """Current backend name: 'gemini' or 'nvidia'."""
    return (os.getenv("LLM_PROVIDER", "gemini") or "gemini").strip().lower()


def is_nvidia() -> bool:
    return provider() == "nvidia"


def api_key() -> str:
    if is_nvidia():
        return os.getenv("NVIDIA_API_KEY", "") or ""
    return os.getenv("GEMINI_API_KEY", "") or os.getenv("GOOGLE_API_KEY", "") or ""


def model() -> str:
    if is_nvidia():
        return os.getenv("NVIDIA_MODEL", NVIDIA_DEFAULT_MODEL) or NVIDIA_DEFAULT_MODEL
    return os.getenv("GEMINI_MODEL", GEMINI_DEFAULT_MODEL) or GEMINI_DEFAULT_MODEL


def provider_display() -> str:
    if is_nvidia():
        return "NVIDIA NIM"
    return "Google Gemini"


def setup_hint() -> str:
    """Human text explaining where to get the key for the active provider."""
    if is_nvidia():
        return (
            "No NVIDIA_API_KEY in .env. Free key (starts with 'nvapi-') at:\n"
            "  https://build.nvidia.com  -> pick a model -> 'Get API Key'\n"
            "Then set LLM_PROVIDER=nvidia and NVIDIA_API_KEY=... in .env"
        )
    return (
        "No GEMINI_API_KEY in .env. Free key at https://aistudio.google.com/apikey\n"
        "(older keys start with 'AIza', newer ones with 'AQ.' - both work)."
    )


def browser_chat(model_name: str | None = None, key: str | None = None):
    """Build the chat model object expected by browser-use's ``Agent``.

    browser-use 0.13.x only accepts its own chat classes (they expose a
    ``.provider`` attribute); raw langchain models are rejected. For NVIDIA we
    use browser-use's OpenAI-compatible ``ChatOpenAI`` pointed at NVIDIA's
    endpoint. For Gemini we use its ``ChatGoogle``.
    """
    model_name = model_name or model()
    key = key or api_key()
    if is_nvidia():
        from browser_use.llm.models import ChatOpenAI

        return ChatOpenAI(
            model=model_name,
            api_key=key,
            base_url=NVIDIA_BASE_URL,
            temperature=0.1,
        )
    from browser_use import ChatGoogle

    return ChatGoogle(model=model_name, api_key=key)
