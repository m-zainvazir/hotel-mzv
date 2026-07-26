"""Chat-model factory.

The graph asks for "a chat model"; this module decides which vendor that is.
Groq (Llama 3.3 70B) is the default for speed and cost; gpt-4o-mini is the
config-level fallback for hard multi-tool turns (plan §17).
"""

from __future__ import annotations

import logging
from functools import lru_cache

from langchain_core.language_models import BaseChatModel

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

_override: BaseChatModel | None = None


class LLMNotConfiguredError(RuntimeError):
    pass


def set_llm_override(model: BaseChatModel | None) -> None:
    """Inject a chat model — used by tests and by offline demo mode."""
    global _override
    _override = model
    _build_llm.cache_clear()


def get_llm() -> BaseChatModel:
    if _override is not None:
        return _override
    settings = get_settings()
    return _build_llm(settings.llm_provider)


@lru_cache(maxsize=4)
def _build_llm(provider: str) -> BaseChatModel:
    settings = get_settings()
    if provider == "groq":
        return _build_groq(settings)
    if provider == "openai":
        return _build_openai(settings)
    if provider == "google":
        return _build_google(settings)
    raise LLMNotConfiguredError(f"unknown LLM_PROVIDER={provider!r}")


def _build_groq(settings: Settings) -> BaseChatModel:
    if not settings.groq_api_key:
        raise LLMNotConfiguredError(
            "GROQ_API_KEY is not set. Copy .env.example to .env and add your key "
            "from https://console.groq.com/keys"
        )
    try:
        from langchain_groq import ChatGroq
    except ImportError as exc:  # pragma: no cover
        raise LLMNotConfiguredError("pip install langchain-groq") from exc

    logger.debug("building ChatGroq model=%s", settings.groq_model)
    return ChatGroq(
        model=settings.groq_model,
        api_key=settings.groq_api_key,
        temperature=settings.llm_temperature,
        max_tokens=settings.llm_max_tokens,
        # Streaming is non-negotiable — Vapi starts speaking on partial text.
        streaming=True,
        # See `llm_max_retries`: the SDK's own backoff sleeps for 3-18s on a
        # 429, which reads as a dead line to the caller and gets the call
        # killed. We do our own single, immediate retry where it can help.
        max_retries=0,
        timeout=settings.llm_timeout_seconds,
    )


def _build_google(settings: Settings) -> BaseChatModel:  # pragma: no cover - optional extra
    """Google Gemini.

    Gemini's free tier has far higher request/token limits than Groq's, and it
    supports tool calling well — so the booking critical path holds up. Same
    streaming contract, same tool interface; the graph doesn't know the
    difference.
    """
    if not settings.google_api_key:
        raise LLMNotConfiguredError(
            "GOOGLE_API_KEY is not set. Get one from https://aistudio.google.com/apikey"
        )
    try:
        from langchain_google_genai import ChatGoogleGenerativeAI
    except ImportError as exc:
        raise LLMNotConfiguredError('pip install -e ".[google]"') from exc

    logger.debug("building ChatGoogleGenerativeAI model=%s", settings.google_model)
    return ChatGoogleGenerativeAI(
        model=settings.google_model,
        google_api_key=settings.google_api_key,
        temperature=settings.llm_temperature,
        # Gemini's SDK names this differently from the OpenAI/Groq clients.
        max_output_tokens=settings.llm_max_tokens,
        max_retries=settings.llm_max_retries,
        timeout=settings.llm_timeout_seconds,
    )


def _build_openai(settings: Settings) -> BaseChatModel:  # pragma: no cover - optional extra
    """OpenAI, or anything that speaks its API.

    `OPENAI_BASE_URL` points this at DeepSeek, Zhipu/GLM, Qwen, OpenRouter,
    Together or a local Ollama without a line of code changing — they all
    implement the same `/chat/completions` contract, including tool calls.
    """
    if not settings.openai_api_key:
        raise LLMNotConfiguredError("OPENAI_API_KEY is not set.")
    try:
        from langchain_openai import ChatOpenAI
    except ImportError as exc:
        raise LLMNotConfiguredError('pip install -e ".[openai]"') from exc

    logger.debug(
        "building ChatOpenAI model=%s base_url=%s",
        settings.openai_model,
        settings.openai_base_url or "https://api.openai.com/v1",
    )
    return ChatOpenAI(
        model=settings.openai_model,
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
        temperature=settings.llm_temperature,
        max_tokens=settings.llm_max_tokens,
        streaming=True,
        max_retries=settings.llm_max_retries,
        timeout=settings.llm_timeout_seconds,
    )
