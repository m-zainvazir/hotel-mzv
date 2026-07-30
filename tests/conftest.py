"""Shared fixtures.

The whole suite runs without a Groq key: `ScriptedChatModel` stands in for the
real model and streams token-by-token, so the streaming path under test is the
same one production uses.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import AsyncIterator, Iterator, Sequence
from typing import Any

import httpx
import pytest
from langchain_core.callbacks import (
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import Field

from app.brain.acknowledge import seed as seed_acknowledgements
from app.brain.graph import reset_graph
from app.brain.llm import set_llm_override
from app.channels.ratelimit import reset_rate_limits
from app.config import Settings, reset_settings_cache
from app.db.auth import clear_jwt_cache
from app.db.memory_store import get_store
from app.mcp.client import clear_mcp_cache
from app.tenancy.loader import clear_tenant_cache, get_repository, get_tenant_config
from app.tenancy.models import TenantConfig
from app.tenancy.secrets import clear_secret_cache
from app.tools.booking.stub import StubBookingProvider
from app.tools.http_client import reset_shared_clients
from app.tools.providers import reset_provider_overrides, set_booking_provider


class ScriptedChatModel(BaseChatModel):
    """Replays a fixed script, streaming each reply word by word.

    Script entries are an `AIMessage`, a callable taking the prompt messages and
    returning one (which lets a test react to a tool result it couldn't know in
    advance, e.g. a generated job id), or an exception to raise (to simulate a
    provider failure).
    """

    responses: list[Any] = Field(default_factory=list)
    cursor: int = 0
    seen_prompts: list[list[BaseMessage]] = Field(default_factory=list)
    bound_tool_names: list[str] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> ScriptedChatModel:
        self.bound_tool_names = [getattr(t, "name", str(t)) for t in tools]
        return self

    # --- scripting ---------------------------------------------------------

    def _next(self, messages: list[BaseMessage]) -> AIMessage:
        self.seen_prompts.append(list(messages))
        if self.cursor >= len(self.responses):
            raise AssertionError(
                f"ScriptedChatModel ran out of responses after {self.cursor} call(s); "
                "the graph asked for one more than the test scripted."
            )
        entry = self.responses[self.cursor]
        self.cursor += 1
        if isinstance(entry, BaseException):  # script a provider failure
            raise entry
        return entry(messages) if callable(entry) else entry

    @staticmethod
    def _chunks(message: AIMessage) -> list[ChatGenerationChunk]:
        chunks: list[ChatGenerationChunk] = []
        text = message.content if isinstance(message.content, str) else ""
        for piece in re.findall(r"\S+\s*", text):
            chunks.append(ChatGenerationChunk(message=AIMessageChunk(content=piece)))

        if message.tool_calls:
            chunks.append(
                ChatGenerationChunk(
                    message=AIMessageChunk(
                        content="",
                        tool_call_chunks=[
                            {
                                "name": call["name"],
                                "args": json.dumps(call["args"]),
                                "id": call.get("id") or f"call_{index}",
                                "index": index,
                            }
                            for index, call in enumerate(message.tool_calls)
                        ],
                    )
                )
            )
        if not chunks:  # a genuinely empty reply still needs one chunk
            chunks.append(ChatGenerationChunk(message=AIMessageChunk(content="")))
        return chunks

    # --- BaseChatModel -----------------------------------------------------

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self._next(messages))])

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        yield from self._chunks(self._next(messages))

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        for chunk in self._chunks(self._next(messages)):
            yield chunk


def ai(content: str = "", tool_calls: list[dict[str, Any]] | None = None) -> AIMessage:
    """Build one scripted assistant turn."""
    calls = [
        {"name": c["name"], "args": c.get("args", {}), "id": c.get("id") or f"call_{i}"}
        for i, c in enumerate(tool_calls or [])
    ]
    return AIMessage(content=content, tool_calls=calls)


@pytest.fixture(autouse=True, scope="session")
def hermetic_settings():
    """Isolate the suite from the machine it runs on.

    Learned the hard way: setting a real `VAPI_WEBHOOK_SECRET` in `.env` made 28
    tests fail, because the suite was quietly inheriting whatever was on the
    developer's box. A test must depend only on what it sets itself — otherwise
    it reports on someone's laptop rather than on the code, and CI disagrees
    with local for reasons nobody can see.

    So both channels of ambient config are cut: the `.env` file, and any
    exported variable matching a settings field. `monkeypatch.setenv` still
    works normally for tests that deliberately exercise configuration.
    """
    original_env_file = Settings.model_config.get("env_file")
    Settings.model_config["env_file"] = None

    stashed = {
        key: os.environ.pop(key)
        for key in [name.upper() for name in Settings.model_fields]
        if key in os.environ
    }
    reset_settings_cache()

    yield

    Settings.model_config["env_file"] = original_env_file
    os.environ.update(stashed)
    reset_settings_cache()


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Hard guard: no test may open a real socket.

    Providers (Cal.com, Twilio) talk over `httpx.AsyncClient`; tests must
    inject a `transport=httpx.MockTransport(...)` instead of hitting the
    network. `MockTransport` subclasses `AsyncBaseTransport` directly, not
    `AsyncHTTPTransport`, so this guard doesn't interfere with it.

    Declared *before* `isolated_runtime` on purpose (Phase 4): within one
    scope, autouse fixtures run in declaration order, and `isolated_runtime`
    walks `get_repository().list_ids()` — a real network call once tenant
    config can be Supabase-backed. Declaring the guard first means that call
    fails loudly here instead of every test in the suite quietly opening a
    socket before the guard is armed.
    """

    def _blocked(*_args, **_kwargs):
        raise AssertionError(
            "a test tried to open a real socket — inject a httpx.MockTransport instead"
        )

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _blocked)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _blocked)


@pytest.fixture(autouse=True)
def isolated_runtime():
    """Every test starts with an empty store, cold caches and no LLM override."""
    from app.tenancy import loader

    reset_settings_cache()
    get_store().reset()
    clear_tenant_cache()
    # Phase 8: nothing else resets the module-global repository between
    # tests — only `override_tenant` saves/restores it, opt-in per test. The
    # moment any test installs a SupabaseTenantRepository (or any other
    # double) and forgets to restore it, every later test in the session
    # silently inherits it, and `no_network` above turns that into dozens of
    # unrelated failures with a misleading message. Force every test back to
    # the default JsonFileTenantRepository.
    loader.set_repository(None)
    reset_provider_overrides()
    # Tests must never depend on a committed tenant's `booking.provider` —
    # pin every real tenant (hotel-mzv, northside-plumbing) to the in-process
    # stub regardless of what its JSON says, so a tenant flipped to "calcom"
    # for production (hotel-mzv is, once real credentials exist) can never
    # make the suite dial out. `no_network` above would catch it anyway, but
    # loudly failing dozens of unrelated tests is worse than never trying.
    # Tests that specifically exercise Cal.com either construct
    # `CalcomBookingProvider` directly (test_calcom_booking.py — bypasses
    # this entirely) or explicitly clear/replace this pin themselves
    # (test_calcom_tools.py::test_get_booking_provider_dispatches_calcom).
    for tenant_id in get_repository().list_ids():
        set_booking_provider(tenant_id, StubBookingProvider())
    reset_graph()
    set_llm_override(None)
    reset_shared_clients()
    clear_jwt_cache()
    clear_secret_cache()
    clear_mcp_cache()
    reset_rate_limits()
    seed_acknowledgements(1234)
    yield
    reset_settings_cache()
    get_store().reset()
    loader.set_repository(None)
    reset_graph()
    set_llm_override(None)
    reset_provider_overrides()
    reset_rate_limits()
    reset_shared_clients()
    clear_jwt_cache()
    clear_secret_cache()
    clear_mcp_cache()


@pytest.fixture
def hotel():
    return get_tenant_config("hotel-mzv")


@pytest.fixture
def northside():
    return get_tenant_config("northside-plumbing")


@pytest.fixture
def scripted():
    """Install a scripted model and hand the test a way to load its script."""

    def _install(*responses: Any) -> ScriptedChatModel:
        model = ScriptedChatModel(responses=list(responses))
        set_llm_override(model)
        reset_graph()
        return model

    return _install


def tool_config(tenant_id: str, channel: str = "chat") -> dict:
    """The RunnableConfig shape tools expect."""
    return {"configurable": {"tenant_id": tenant_id, "channel": channel}}


@pytest.fixture
def override_tenant():
    """Register a modified TenantConfig so tool/graph code sees it.

    A bare `hotel.model_copy(update={...})` is invisible to tool code: every
    tool re-resolves its tenant via `get_tenant_config(tenant_id)`, which reads
    the cached repository, not whatever local object a test happens to hold.
    This registers an override on that repository instead — the seam
    `app.tenancy.loader.set_repository` documents as being for exactly this.
    """
    from app.tenancy import loader

    original_repository = loader._repository
    base = loader.get_repository()
    overrides: dict[str, TenantConfig] = {}

    class _OverrideRepository:
        def get(self, tenant_id: str) -> TenantConfig:
            return overrides.get(tenant_id) or base.get(tenant_id)

        def list_ids(self) -> list[str]:
            return base.list_ids()

        def find_by_phone(self, phone_number: str) -> TenantConfig | None:
            return base.find_by_phone(phone_number)

        def find_by_widget_key(self, widget_key: str) -> TenantConfig | None:
            return base.find_by_widget_key(widget_key)

        def find_by_assistant_id(self, assistant_id: str) -> TenantConfig | None:
            return base.find_by_assistant_id(assistant_id)

    def _register(config: TenantConfig) -> None:
        overrides[config.tenant_id] = config
        loader.set_repository(_OverrideRepository())

    yield _register

    loader.set_repository(original_repository)


def mock_http(
    handler, *, auth=None, headers: dict[str, str] | None = None
) -> tuple[httpx.AsyncClient, list[httpx.Request]]:
    """A client whose every request goes to `handler`, plus captured requests.

    `handler(request) -> httpx.Response`. Use for Cal.com/Twilio provider
    tests: build the client here, inject it via the provider's `client=`
    constructor argument, then assert on the captured `httpx.Request` objects
    (url, params, headers, json/form body) as well as on return values. Pass
    `auth=(sid, token)` / `headers={...}` to mirror what `shared_async_client`
    sets up for real requests — an injected client otherwise carries neither.
    """
    captured: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return handler(request)

    transport = httpx.MockTransport(_handler)
    client = httpx.AsyncClient(
        transport=transport, base_url="https://example.invalid", auth=auth, headers=headers
    )
    return client, captured
