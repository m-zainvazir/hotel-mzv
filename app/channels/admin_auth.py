"""Admin authentication — operator-only today, shaped for tenant login later.

Every other guard in `app/channels/security.py` fails *open* when its secret
is unset — the right default for a zero-config dev box. This one is the
deliberate exception: an unauthenticated `/chat` costs one Groq request; an
unauthenticated admin request rewrites every tenant's config (including
`emergency.escalation_phone` and `booking.event_type_id`) and reads every
call/chat transcript. `ADMIN_ENABLED=false` by default means every admin
route 404s via `require_admin_enabled` below — the observable effect of "the
router isn't mounted" (a 404, not a 401) without actually conditionally
calling `include_router`, which can't cleanly toggle on/off against the one
`app.main.app` object every test in the suite shares (a router, once
included, has no clean "un-include"). `require_admin` then 401s on every
request when `ADMIN_AUTH_TOKEN` is unset, rather than falling open the way
`require_chat_caller` does.

`AdminPrincipal` is the abstraction the "designed for tenant login later" bet
rests on. Today `require_admin` has exactly one branch: the shared bearer ->
`kind="operator"`, `tenant_ids=None` (every tenant). When real per-tenant
login lands (`plans/phase10.md` item 14), it gains a second branch — a
verified Supabase Auth JWT -> `kind="tenant"`, `tenant_ids=(that tenant,)` —
and every route below keeps working unchanged, because none of them ever
inspect the raw token: they all depend on `require_tenant_access`, which
only ever asks the principal "may you touch this tenant_id".
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Literal

from fastapi import Depends, Header, HTTPException, Request, status

from app.channels import ratelimit
from app.config import get_settings

#: A failed bearer costs a caller time before it costs them another guess —
#: independent of `admin_requests_per_minute` (app/channels/ratelimit.py),
#: which throttles *all* admin traffic including successfully authenticated
#: requests. This throttles only failures, so a legitimate high-frequency
#: operator never trips it via their own valid calls.
_FAILED_AUTH_LIMIT = 10
_FAILED_AUTH_WINDOW_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class AdminPrincipal:
    kind: Literal["operator", "tenant"]
    #: `None` means "every tenant" (the operator today). A future tenant
    #: principal carries exactly the tenant ids its login maps to.
    tenant_ids: tuple[str, ...] | None
    #: "operator" today; a Supabase Auth user id once tenant login exists.
    subject: str

    def may_access(self, tenant_id: str) -> bool:
        return self.tenant_ids is None or tenant_id in self.tenant_ids

    def may_write(self, tenant_id: str) -> bool:
        # Field-level write restrictions (`_OPERATOR_ONLY_PATHS`,
        # app/tenancy/admin.py, Step 6) are a separate, finer-grained check —
        # this is only the tenant-level gate. Kept as its own method rather
        # than aliasing `may_access` because a future role (e.g. a
        # read-only tenant viewer) would diverge here without touching any
        # caller of `may_access`.
        return self.may_access(tenant_id)


def require_admin_enabled() -> None:
    """Every admin route depends on this first (`app/channels/admin.py`'s
    router-level `dependencies=`). `ADMIN_ENABLED=false` — the default —
    produces a plain 404 here, before the bearer is even inspected, which
    is indistinguishable from the route never having existed."""
    if not get_settings().admin_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")


async def require_admin(
    request: Request, authorization: str | None = Header(default=None)
) -> AdminPrincipal:
    """Resolve the caller as an `AdminPrincipal`, or 401.

    Deliberately does NOT accept `API_AUTH_TOKEN` — that token's power is
    "run a conversation as any tenant" (`app/channels/security.py`), and
    reusing it here would silently promote every existing holder (dev
    boxes, `chat_cli`, tests, CI) to full admin read/write, a privilege
    escalation via a config change with no code review attached.
    """
    settings = get_settings()
    expected = settings.admin_auth_token
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="admin auth is not configured (ADMIN_AUTH_TOKEN unset)",
        )

    presented = ""
    if authorization and authorization.lower().startswith("bearer "):
        presented = authorization[7:].strip()

    if presented and hmac.compare_digest(presented, expected):
        return AdminPrincipal(kind="operator", tenant_ids=None, subject="operator")

    retry_after = ratelimit._hit(
        "admin-auth-fail",
        ratelimit._client_ip(request),
        limit=_FAILED_AUTH_LIMIT,
        window_seconds=_FAILED_AUTH_WINDOW_SECONDS,
    )
    if retry_after is not None:
        raise ratelimit._too_many_requests(retry_after)

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="invalid or missing bearer token",
    )


async def require_tenant_access(
    tenant_id: str, principal: AdminPrincipal = Depends(require_admin)
) -> AdminPrincipal:
    """The dependency every tenant-scoped admin route uses instead of
    `require_admin` directly — the tenant id is bound from the route's own
    `{tenant_id}` path parameter, so authorization is one comparison in one
    place regardless of how many routes exist."""
    if not principal.may_access(tenant_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"no access to tenant {tenant_id!r}",
        )
    return principal
