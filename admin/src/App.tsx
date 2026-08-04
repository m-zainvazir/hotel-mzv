import { useEffect, useState } from "preact/hooks";

import {
  AuthError,
  clearToken,
  getSession,
  getToken,
  listTenantIds,
  SessionInfo,
  setToken,
  TenantSummary,
} from "./api";
import { newTenantUrl, Route, tenantUrl, useRoute } from "./router";
import { NewTenantView } from "./views/NewTenant";
import { Overview } from "./views/Overview";
import { TenantView } from "./views/TenantView";

export function App() {
  const [session, setSession] = useState<SessionInfo | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const route = useRoute();

  async function loadSession(): Promise<void> {
    setLoading(true);
    try {
      const info = await getSession();
      setSession(info);
      setError(null);
    } catch (err) {
      setSession(null);
      if (!(err instanceof AuthError)) {
        setError((err as Error).message);
      }
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    if (getToken()) {
      void loadSession();
    } else {
      setLoading(false);
    }
  }, []);

  if (loading) {
    return (
      <div class="admin-login">
        <p class="admin-muted">Loading…</p>
      </div>
    );
  }

  if (!session) {
    return (
      <LoginScreen
        error={error}
        onLogin={(token) => {
          setToken(token);
          void loadSession();
        }}
      />
    );
  }

  function signOut(): void {
    clearToken();
    setSession(null);
  }

  return (
    <div class="admin-shell">
      <Sidebar route={route} session={session} onSignOut={signOut} />
      <main class="admin-main">
        {route.name === "overview" && <Overview />}
        {route.name === "new-tenant" && <NewTenantView />}
        {route.name === "tenant" && (
          <TenantView tenantId={route.tenantId} tab={route.tab} session={session} />
        )}
      </main>
    </div>
  );
}

function LoginScreen({
  error,
  onLogin,
}: {
  error: string | null;
  onLogin: (token: string) => void;
}) {
  const [value, setValue] = useState("");
  return (
    <div class="admin-login">
      <h1>AI Receptionist — Admin</h1>
      {error && <div class="admin-error-banner">{error}</div>}
      <form
        onSubmit={(event) => {
          event.preventDefault();
          const trimmed = value.trim();
          if (trimmed) onLogin(trimmed);
        }}
      >
        <div class="admin-field">
          <label for="admin-token">Admin bearer token</label>
          <input
            id="admin-token"
            type="password"
            value={value}
            onInput={(event) => setValue((event.target as HTMLInputElement).value)}
            autofocus
          />
        </div>
        <button class="admin-btn" type="submit" disabled={!value.trim()}>
          Sign in
        </button>
      </form>
    </div>
  );
}

function Sidebar({
  route,
  session,
  onSignOut,
}: {
  route: Route;
  session: SessionInfo;
  onSignOut: () => void;
}) {
  const [tenants, setTenants] = useState<TenantSummary[]>([]);
  const [showArchived, setShowArchived] = useState(false);

  function reload(): void {
    listTenantIds()
      .then((body) => setTenants(body.tenants))
      .catch(() => setTenants([]));
  }

  useEffect(reload, []);
  // A create/archive/restore/purge action elsewhere in the app changes this
  // list — cheapest correct fix is reloading on every route change rather
  // than plumbing a refresh callback through three views.
  useEffect(reload, [route]);

  const live = tenants.filter((t) => t.status !== "archived");
  const archived = tenants.filter((t) => t.status === "archived");

  function tenantLink(tenant: TenantSummary) {
    return (
      <a
        key={tenant.tenant_id}
        href={tenantUrl(tenant.tenant_id)}
        class={route.name === "tenant" && route.tenantId === tenant.tenant_id ? "active" : ""}
      >
        {tenant.tenant_id}
      </a>
    );
  }

  return (
    <nav class="admin-sidebar">
      <h1>AI Receptionist</h1>
      <a href="#/" class={route.name === "overview" ? "active" : ""}>
        Overview
      </a>
      {session.kind === "operator" && (
        <a href={newTenantUrl()} class={route.name === "new-tenant" ? "active" : ""}>
          + New bot
        </a>
      )}
      <div class="admin-muted" style={{ margin: "0.75rem 0 0.25rem", fontSize: "0.72rem" }}>
        Tenants
      </div>
      {live.map(tenantLink)}
      {archived.length > 0 && (
        <>
          <button
            class="admin-nav-link admin-muted"
            style={{ fontSize: "0.72rem", textAlign: "left" }}
            onClick={() => setShowArchived((v) => !v)}
          >
            {showArchived ? "▾" : "▸"} Archived ({archived.length})
          </button>
          {showArchived && archived.map(tenantLink)}
        </>
      )}
      <div style={{ flex: 1 }} />
      <div class="admin-muted" style={{ fontSize: "0.75rem" }}>
        Signed in as {session.kind}
      </div>
      <button class="admin-nav-link" onClick={onSignOut}>
        Sign out
      </button>
    </nav>
  );
}
