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
    #: Which tenant repository backs `app/tenancy/loader.py`. Stays "json" by
    #: default even once Supabase is configured — see plan's "On the tenant
    #: read path" note; flipping to "supabase" is a one-line config change
    #: made only after Step 4 is verified live.
    tenant_source: Literal["json", "supabase"] = "json"
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
