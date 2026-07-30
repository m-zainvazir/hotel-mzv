import { useEffect, useState } from "preact/hooks";

import { AuthError, clearToken, getSession, getToken, listTenantIds, SessionInfo, setToken } from "./api";
import { Route, tenantUrl, useRoute } from "./router";
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
        {route.name === "overview" ? (
          <Overview />
        ) : (
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
  const [tenantIds, setTenantIds] = useState<string[]>([]);

  useEffect(() => {
    listTenantIds()
      .then((body) => setTenantIds(body.tenant_ids))
      .catch(() => setTenantIds([]));
  }, []);

  return (
    <nav class="admin-sidebar">
      <h1>AI Receptionist</h1>
      <a href="#/" class={route.name === "overview" ? "active" : ""}>
        Overview
      </a>
      <div class="admin-muted" style={{ margin: "0.75rem 0 0.25rem", fontSize: "0.72rem" }}>
        Tenants
      </div>
      {tenantIds.map((tenantId) => (
        <a
          key={tenantId}
          href={tenantUrl(tenantId)}
          class={route.name === "tenant" && route.tenantId === tenantId ? "active" : ""}
        >
          {tenantId}
        </a>
      ))}
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
