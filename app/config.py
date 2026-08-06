"""Application settings.

Everything configurable lives here and comes from the environment, never from
code (CLAUDE.md convention #5). Nothing in this module is vendor-specific
beyond naming the env vars — the brain reads `Settings`, not provider SDKs.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- app ---------------------------------------------------------------
    app_env: Literal["development", "staging", "production"] = "development"
    log_level: str = "INFO"
    #: "json" in production — a structured record per line, correlated by
    #: request id (app/middleware.py). "text" stays the human-readable
    #: default for a dev console.
    log_format: Literal["text", "json"] = "text"
    host: str = "0.0.0.0"
    port: int = 8000
    api_auth_token: str | None = None

    # --- reasoning model ---------------------------------------------------
    llm_provider: Literal["groq", "openai", "google"] = "groq"
    groq_api_key: str | None = None
    groq_model: str = "llama-3.3-70b-versatile"
    llm_temperature: float = 0.2
    llm_max_tokens: int = 1024
    #: Provider-SDK retries. Default 0 on purpose: the Groq client's built-in
    #: backoff waits 3-18s before raising, and on a live call Vapi gives up long
    #: before that ("error-providerfault-custom-llm-llm-failed"). Failing fast
    #: into a spoken apology beats twenty seconds of silence.
    llm_max_retries: int = 0
    #: Hard ceiling on one provider call. Must stay well under Vapi's patience.
    llm_timeout_seconds: float = 20.0
    #: Conversation messages kept per request. History is re-sent every time, so
    #: unbounded growth is a direct, compounding token cost on long calls.
    llm_history_messages: int = 20
    openai_api_key: str | None = None
    openai_model: str = "gpt-4o-mini"
    #: Any OpenAI-compatible endpoint — DeepSeek, Zhipu/GLM, Qwen (DashScope),
    #: OpenRouter, Together, a local Ollama. Leave unset for OpenAI itself.
    #: This is the cheap-model escape hatch: provider swaps cost one env var.
    openai_base_url: str | None = None

    # --- Google Gemini (much higher free-tier limits than Groq) ------------
    google_api_key: str | None = None
    google_model: str = "gemini-2.5-flash-lite"
    #: The chat model itself goes through `langchain-google-genai`, which
    #: knows its own endpoint — this is only for `app/rag/embeddings.py`'s
    #: raw httpx call (Phase 9 Part C), matching this project's "raw httpx,
    #: no SDKs" precedent for everything that isn't the reasoning model
    #: itself. Same value `scripts/check_model.py` already hardcodes as
    #: GOOGLE_BASE — real, not a placeholder.
    google_api_base: str = "https://generativelanguage.googleapis.com/v1beta"

    # --- tenancy -----------------------------------------------------------
    default_tenant_id: str = "hotel-mzv"
    tenant_cache_ttl_seconds: int = 300

    # NOTE: there is deliberately no global `booking_provider` /
    # `notifier_provider` setting. Which provider a tenant uses is decided by
    # `tenant.booking.provider` / `tenant.notifications.provider` in that
    # tenant's own JSON (see `app/tools/providers.py`) — a global default here
    # would be a second, contradictable source of truth for the same decision.

    # --- voice channel: Vapi + STT/TTS (Phase 2) ---------------------------
    vapi_private_key: str | None = None
    vapi_public_key: str | None = None
    #: Shared secret Vapi presents on server requests, as `x-vapi-secret` or as
    #: an HMAC-SHA256 `x-vapi-signature` over the raw body — which one depends
    #: on how the assistant's `server` block is configured, so we accept both.
    vapi_webhook_secret: str | None = None
    vapi_api_base: str = "https://api.vapi.ai"
    #: Public HTTPS origin Vapi calls back on (ngrok in dev).
    public_base_url: str | None = None

    cartesia_api_key: str | None = None
    #: Fallback voice for tenants that don't pin their own `voice.voice_id`.
    cartesia_default_voice_id: str | None = None
    cartesia_api_base: str = "https://api.cartesia.ai"
    #: Cartesia's dated API version header. Observed in their OpenAPI spec at
    #: writing (Phase 4 Step 8) — confirm against docs.cartesia.ai before a
    #: real clone; a wrong value 400s cleanly and is a one-line env fix.
    cartesia_api_version: str = "2026-03-01"
    #: Separate from every other timeout in this app on purpose — this is a
    #: multipart audio upload, not a quick API call. Reusing a booking-shaped
    #: timeout would silently fail a real clone attempt.
    cartesia_clone_timeout_seconds: float = 60.0
    deepgram_api_key: str | None = None

    # --- booking: Cal.com (Phase 3) -----------------------------------------
    calcom_api_key: str | None = None
    calcom_api_base: str = "https://api.cal.com/v2"
    #: Shared by the slots GET (fast) and the booking POST (slower — Cal.com
    #: creates the calendar event, video link and notifications synchronously
    #: before responding; verified live that 8s was too tight and produced a
    #: false-negative timeout on an otherwise-successful booking).
    calcom_timeout_seconds: float = 15.0
    #: Cal.com requires an attendee email; phone callers rarely give one. When
    #: none is offered we synthesize `caller-<digits>@<this domain>` —
    #: deterministic (same caller twice = same Cal.com attendee). Must be a
    #: domain with real MX records: Cal.com actively validates attendee email
    #: deliverability (verified live — a made-up domain gets a flat 400,
    #: "This email address cannot receive mail"), so `example.com` (IANA's
    #: reserved, always-resolvable documentation domain) is the safe default
    #: rather than a made-up one.
    booking_placeholder_email_domain: str = "example.com"

    # --- booking: Cal.com over MCP (Phase 9 Part A) -------------------------
    #: The official hosted Cal.com MCP server — streamable HTTP, OAuth 2.1
    #: only (no API-key path). Confirmed live (Step A0 spike, 2026-08-01):
    #: `/.well-known/oauth-protected-resource` and
    #: `/.well-known/oauth-authorization-server` both resolve at this origin,
    #: Dynamic Client Registration (RFC 7591) is open at `/oauth/register`
    #: with no pre-shared credential, and `grant_types_supported` includes
    #: `refresh_token` — everything headless in `app/mcp/oauth.py` depends on
    #: that last one. `token_endpoint_auth_methods_supported: ["none"]` means
    #: DCR returns a bare `client_id`, no secret — a public client using PKCE
    #: (S256) is the whole story, not a fallback path.
    calcom_mcp_url: str = "https://mcp.cal.com/mcp"
    #: Per-tool-call ceiling once a session is open, mirroring
    #: `calcom_timeout_seconds` — booking is synchronous the same way it is
    #: through the REST provider.
    calcom_mcp_timeout_seconds: float = 15.0
    #: Bounds opening the streamable-HTTP session + `initialize()` only, kept
    #: separate from the call timeout above for the same reason
    #: `mcp_connect_timeout_seconds` is separate from `mcp_tool_timeout_seconds`.
    calcom_mcp_connect_timeout_seconds: float = 10.0
    #: How long a resolved access token is trusted before
    #: `app/mcp/oauth.py::access_token_for` refreshes it again, checked
    #: against the token response's own `expires_in` — this is a poll
    #: interval against that real expiry, the same relationship
    #: `app/db/auth.py::tenant_jwt`'s 60s cache has to its JWT's longer one.
    calcom_oauth_token_cache_seconds: int = 60
    #: `scripts/authorize_calcom.py`'s temporary localhost callback listener,
    #: used once per tenant during the interactive authorization-code grant.
    calcom_oauth_redirect_port: int = 8901
    #: How long `McpBookingProvider` (app/tools/booking/mcp_calcom.py) keeps a
    #: connected MCP session before reconnecting — a fresh streamable-HTTP
    #: handshake per `check_availability` call would blow the §13 latency
    #: budget, the same correctness-and-latency argument
    #: `mcp_tool_cache_ttl_seconds` makes for the tenant long-tail tool list.
    calcom_mcp_session_cache_ttl_seconds: float = 300.0

    # --- telephony: Twilio (Phase 3) ----------------------------------------
    twilio_account_sid: str | None = None
    twilio_auth_token: str | None = None
    twilio_from_number: str | None = None
    twilio_api_base: str = "https://api.twilio.com"
    #: Preferred over `twilio_from_number` when set — the routing Twilio wants
    #: for A2P-registered traffic.
    twilio_messaging_service_sid: str | None = None
    twilio_whatsapp_from: str | None = None
    twilio_timeout_seconds: float = 8.0

    # --- database: Supabase (Phase 4) ---------------------------------------
    #: PostgREST base URL, e.g. https://<ref>.supabase.co. Unset means the
    #: in-memory store/tenant repository stay active — see `app/db/factory.py`.
    supabase_url: str | None = None
    #: Publishable/anon key. Sent as the mandatory `apikey` header alongside
    #: the per-request tenant JWT `Authorization` header (see `app/db/auth.py`).
    supabase_anon_key: str | None = None
    #: Secret/service_role key. Bypasses RLS — reserved for admin paths only
    #: (onboarding, `sync_tenants.py`), never for a tenant-scoped request.
    supabase_secret_key: str | None = None
    #: Legacy HS256 shared secret, used to mint short-lived per-tenant JWTs so
    #: RLS policies can read `auth.jwt() ->> 'tenant_id'`. Confirmed present or
    #: absent per-project before Step 5 is built — see plan Risk 1.
    supabase_jwt_secret: str | None = None
    supabase_timeout_seconds: float = 8.0
    #: TTL for cached per-tenant secrets read from Vault (`app/tenancy/secrets.py`).
    secret_cache_ttl_seconds: int = 300
    #: Which tenant repository backs `app/tenancy/loader.py`. Pulled forward
    #: to "supabase" in production by Phase 8: an admin panel that edits
    #: config while this stays "json" produces the phantom edit
    #: (app/preflight.py refuses that combination). "json" stays the default
    #: because it's what every test and dev box needs — see
    #: `content/tenants/*.json`, now seed + boot fallback rather than runtime
    #: truth once this is "supabase".
    tenant_source: Literal["json", "supabase"] = "json"
    #: How often `SupabaseTenantRepository` (Phase 8) re-loads the whole
    #: tenant registry in the background, on top of the explicit refresh an
    #: admin write triggers. Not a latency knob — it's the self-healing path
    #: after a wholesale load failure, and the only way a direct SQL/
    #: `sync_tenants.py` edit (bypassing the admin API) is ever picked up by
    #: a running server. 0 disables the background loop.
    tenant_snapshot_refresh_seconds: int = 300
    #: Timeout for the boot-time tenant snapshot specifically — deliberately
    #: NOT `supabase_timeout_seconds`. That one is shaped for per-request
    #: business queries on the latency budget; this single query runs once at
    #: boot, on a cold process whose very first HTTPS call also pays DNS + TLS
    #: to a cross-continental region. Sharing the 8s request-shaped budget
    #: made a cold boot intermittently time out and silently serve the baked-in
    #: JSON fallback for up to `tenant_snapshot_refresh_seconds` — observed
    #: live on a Railway-shaped cold start. Same reasoning `plans/phase4.md`
    #: gave the Cartesia clone upload its own long timeout rather than reusing
    #: the booking-shaped one.
    tenant_snapshot_timeout_seconds: float = 20.0
    #: Supavisor SESSION-mode pooler URI (port 5432, not 6543) for the
    #: LangGraph Postgres checkpointer. Unset means `InMemorySaver` stays
    #: active. Never the direct `db.<ref>.supabase.co` host — it is IPv6-only.
    database_url: str | None = None
    checkpoint_retention_hours: int = 48

    # --- mcp (Phase 6) -----------------------------------------------------
    mcp_enabled: bool = False
    #: Per-tool-call ceiling once a session is open. Distinct from
    #: `mcp_connect_timeout_seconds` below, which bounds only the handshake +
    #: `get_tools()` — a slow *call* (e.g. a scraper) is a different failure
    #: mode from a slow *connect*, and conflating them would make a slow tool
    #: call kill a fast server's connection budget too.
    mcp_tool_timeout_seconds: int = 15
    #: Bounds `MultiServerMCPClient.get_tools()` per server. One dead
    #: third-party server must never stall a live call — the whole point of
    #: loading servers independently in app/mcp/client.py.
    mcp_connect_timeout_seconds: float = 5.0
    #: Which tenant repository backs `app/mcp/registry.py`. Stays "json" by
    #: default (dev + tests read content/tenants/*.json), but — unlike
    #: `tenant_source` — is safe to flip to "supabase" without a live-verification
    #: pass of its own: the registry read only ever happens inside
    #: `load_mcp_tools`, which is already async, already gated on
    #: `mcp_enabled`, and already degrades to [] on any failure. No autouse
    #: test fixture touches it at collection time the way
    #: `get_repository().list_ids()` does for tenant config.
    mcp_source: Literal["json", "supabase"] = "json"
    #: A `command` string in tenant config is arbitrary code execution on the
    #: one box holding every tenant's data, and most hosted deploys can't
    #: spawn a subprocess anyway. Off by default; an operator opts in
    #: explicitly for a local stdio server.
    mcp_allow_stdio: bool = False
    #: TTL for the per-tenant MCP tool-list cache (app/mcp/client.py). Not
    #: just a latency knob: `reason` binds this list and the dynamic `tools`
    #: node (app/brain/nodes/tools.py) executes against it in the same turn —
    #: without a cache, two independent `get_tools()` calls against a flaky
    #: server could disagree, and the model would emit a call nothing can run.
    mcp_tool_cache_ttl_seconds: int = 300
    #: Hard cap on MCP tools bound per turn. Every bound tool schema is
    #: re-sent on every request, forever — see README's cost breakdown — so an
    #: unbounded tenant server list is a direct, compounding token cost on a
    #: path that's supposed to be the long tail, not the fast one.
    mcp_max_tools: int = 8

    # --- chat widget (Phase 5) ----------------------------------------------
    #: HMAC key signing widget session tokens (`app/channels/widget_auth.py`).
    #: Unset means a random per-process key is generated at import — dev works
    #: with zero config, tokens just don't survive a restart. Fails safe,
    #: matching the fail-open-when-unconfigured convention in
    #: `app/channels/security.py`.
    widget_session_secret: str | None = None
    widget_session_ttl_seconds: int = 3600
    #: Mirrors CHECKPOINT_RETENTION_HOURS's reasoning for `calls.transcript`
    #: (Phase 4) — chat transcripts carry guest names and phone numbers.
    chat_transcript_retention_days: int = 30

    # --- test agent link (Phase 9.1, shared with Phase 9.3's voice tester) --
    #: HMAC key signing shareable `/test/{token}` links
    #: (`app/channels/test_links.py`). Unset falls back to
    #: `widget_session_secret`, then a random per-process key — same
    #: three-tier degradation `widget_session_secret` itself uses, so a
    #: leaked test link still can't be replayed as a widget session (or vice
    #: versa) even when both end up on the same fallback secret, since the
    #: two claim sets are entirely different shapes.
    test_link_secret: str | None = None
    test_link_ttl_seconds: int = 86400

    # --- rate limiting (Phase 7) --------------------------------------------
    #: In-process, per-replica (see app/channels/ratelimit.py's docstring for
    #: why that's the deliberate limit, not an oversight).
    rate_limit_enabled: bool = True
    #: Per client IP, on both /chat and /chat/session.
    chat_requests_per_minute: int = 20
    #: Per tenant, on /chat only — protects one tenant's daily LLM budget
    #: (Groq's free tier is ~76 requests/day) from any single widget key.
    chat_requests_per_day: int = 200
    #: Per widget session, on /chat only.
    session_requests_per_hour: int = 30

    # --- admin dashboard (Phase 8) -------------------------------------------
    #: Router is only mounted when this is true (a 404, not a 401, when it
    #: isn't) — makes "is admin exposed?" a grep for one env var rather than
    #: an inference from token presence.
    admin_enabled: bool = False
    #: Deliberately separate from API_AUTH_TOKEN — that token's power is "run
    #: a conversation as any tenant"; this one's is "rewrite any tenant's
    #: config and read every transcript". Unlike every other secret in this
    #: file, an unset value here means 401 on every request, not fail-open —
    #: see app/channels/admin_auth.py.
    admin_auth_token: str | None = None
    #: Per-IP ceiling on /admin/api/*, generous relative to the chat limits
    #: since a single operator is the expected caller, not the public.
    admin_requests_per_minute: int = 120

    # --- knowledge base / RAG (Phase 9 Part C) --------------------------------
    #: Gates `search_knowledge` from ever being bound
    #: (`app/tools/registry.py::native_tools_for`) and the admin knowledge
    #: routes from doing real work — off by default so a deploy that hasn't
    #: opted in pays nothing extra, the same posture `mcp_enabled` already
    #: established for the long-tail tool tier.
    knowledge_enabled: bool = False
    #: Mirrors `llm_provider`'s shape. Only "google" is implemented today —
    #: "openai" is declared so a later embeddings-provider swap is a config
    #: flip, not a refactor (app/rag/embeddings.py dispatches on this).
    embedding_provider: Literal["google", "openai"] = "google"
    embedding_model: str = "gemini-embedding-001"
    #: Gemini's embedding model supports Matryoshka truncation down from its
    #: native 3072 dimensions — 768 keeps pgvector's HNSW index (0011_knowledge.sql)
    #: small without a meaningful retrieval-quality hit at this corpus size.
    embedding_dimensions: int = 768
    #: Separate from `llm_timeout_seconds` — an embedding call batches many
    #: chunks per request during ingestion, a different latency shape than a
    #: single chat completion.
    embedding_timeout_seconds: float = 20.0
    #: Per-upload ceiling (`app/rag/ingest.py`), independent of
    #: `knowledge.max_chunks` (`app/tenancy/models.py`) — this bounds one
    #: file's raw bytes before extraction even runs; that one bounds the
    #: tenant's total indexed corpus afterward.
    knowledge_max_upload_bytes: int = 20 * 1024 * 1024
    #: The one and only backend today — declared as a `Literal["supabase"]`
    #: rather than a bare `str`, the same "swap seam, not a live switch yet"
    #: shape `tenant_source`/`mcp_source` used before a second backend
    #: existed. `app/db/store.py::KnowledgeStore` is the seam a future
    #: Qdrant/Pinecone implementation would slot into.
    knowledge_source: Literal["supabase"] = "supabase"

    # --- observability: LangSmith (Phase 7 Step 7) --------------------------
    #: These three were always in .env.example, but were never real Settings
    #: fields (extra="ignore" swallowed them) and nothing ever called
    #: load_dotenv() -- so pydantic-settings read them from .env without
    #: exporting to os.environ, and LangChain's tracer reads os.environ. The
    #: documented switch did nothing under uvicorn. app/main.py's lifespan
    #: exports these into os.environ before get_graph() is built, which is
    #: the actual fix; being real fields is also what makes them visible to
    #: `/health`'s auth-gated detail and stripped by hermetic_settings.
    langchain_tracing_v2: bool = False
    langchain_api_key: str | None = None
    langchain_project: str = "ai-receptionist"

    @property
    def content_dir(self) -> Path:
        """The single folder holding user-editable content (see content/README.md)."""
        return REPO_ROOT / "content"

    @property
    def tenant_data_dir(self) -> Path:
        return self.content_dir / "tenants"

    @property
    def active_model(self) -> str:
        """The model id for whichever provider is currently selected."""
        return {
            "groq": self.groq_model,
            "openai": self.openai_model,
            "google": self.google_model,
        }.get(self.llm_provider, self.groq_model)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Test hook — drop the memoised Settings so env changes take effect."""
    get_settings.cache_clear()
