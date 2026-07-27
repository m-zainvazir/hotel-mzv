"""Production boot preflight (Phase 7 Step 3).

`app/channels/security.py`'s auth guards and `app/channels/widget_auth.py`'s
session signing all fail *open* when their secret is unset — the right
default for a developer running with zero config, and the wrong one for a
production deploy, where an unset secret means an unauthenticated endpoint
or a session token that can't survive a redeploy. Nothing tied that to
`APP_ENV` before this module. `app/db/factory.py`'s `SUPABASE_URL` check is
the one existing exception and is left where it is; this module covers
everything else.

Deliberately returns every problem instead of raising on the first one —
production boot debugging over SSH/logs is expensive enough without a
"fix one, redeploy, find the next" loop.
"""

from __future__ import annotations

from app.config import Settings

#: DATABASE_URL unset stays a WARNING (logged in `app/brain/graph.py`, and
#: surfaced by `/health`'s `checkpointer` field) — Phase 4 deliberately made
#: checkpointer degradation non-fatal, and this preflight doesn't relitigate
#: that. Everything below IS fatal in production.
_PROVIDER_API_KEY_FIELDS: dict[str, str] = {
    "groq": "groq_api_key",
    "openai": "openai_api_key",
    "google": "google_api_key",
}


def verify_production_settings(settings: Settings) -> list[str]:
    """Return every production-readiness problem found; empty means clean.

    A no-op outside `app_env == "production"` — development and staging keep
    today's fail-open behaviour.
    """
    if settings.app_env != "production":
        return []

    problems: list[str] = []

    if not settings.api_auth_token:
        problems.append(
            "API_AUTH_TOKEN is unset — POST /chat would accept any anonymous "
            "caller as trusted, reading tenant_id from the request body."
        )
    if not settings.vapi_webhook_secret:
        problems.append(
            "VAPI_WEBHOOK_SECRET is unset — /chat/completions and /webhooks/vapi "
            "would accept unauthenticated requests."
        )
    if not settings.widget_session_secret:
        problems.append(
            "WIDGET_SESSION_SECRET is unset — widget session tokens would be "
            "signed with a random per-process key and stop working on every redeploy."
        )
    if not settings.public_base_url:
        problems.append(
            "PUBLIC_BASE_URL is unset — Vapi provisioning has no callback origin to bake in."
        )
    elif not settings.public_base_url.startswith("https://"):
        problems.append(
            f"PUBLIC_BASE_URL={settings.public_base_url!r} is not https:// — "
            "Vapi requires a secure callback origin."
        )
    if settings.supabase_url and not settings.supabase_jwt_secret:
        problems.append(
            "SUPABASE_URL is set but SUPABASE_JWT_SECRET is unset — the first "
            "tenant-scoped query would fail with SupabaseAuthNotConfiguredError."
        )

    key_field = _PROVIDER_API_KEY_FIELDS.get(settings.llm_provider)
    if key_field and not getattr(settings, key_field, None):
        problems.append(
            f"LLM_PROVIDER={settings.llm_provider!r} but "
            f"{key_field.upper()} is unset — every turn would fail to reach the model."
        )

    return problems
