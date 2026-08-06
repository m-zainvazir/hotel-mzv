// A minimal hash router — no routing library. The admin app has exactly two
// shapes of page (the tenant list, and one tenant's tabs), so a ~30-line
// hand-rolled router is simpler and has fewer moving parts than pulling in
// a dependency for it.
import { useEffect, useState } from "preact/hooks";

export type Tab =
  | "metrics"
  | "config"
  | "prompt"
  | "calls"
  | "chats"
  | "jobs"
  | "escalations"
  | "knowledge"
  | "versions";

export type Route =
  | { name: "overview" }
  | { name: "new-tenant" }
  | { name: "tenant"; tenantId: string; tab: Tab };

const VALID_TABS: Tab[] = [
  "metrics",
  "config",
  "prompt",
  "calls",
  "chats",
  "jobs",
  "escalations",
  "knowledge",
  "versions",
];

function parse(hash: string): Route {
  const parts = hash.replace(/^#\/?/, "").split("/").filter(Boolean);
  // Must come before the `tenants/:id` branch below — "new" would otherwise
  // parse as a literal tenant_id (tenantId="new"), matching this app's own
  // "/{tenant_id}" URL shape exactly enough to be a real ambiguity, not a
  // hypothetical one.
  if (parts[0] === "tenants" && parts[1] === "new") {
    return { name: "new-tenant" };
  }
  if (parts[0] === "tenants" && parts[1]) {
    const tab = VALID_TABS.includes(parts[2] as Tab) ? (parts[2] as Tab) : "metrics";
    return { name: "tenant", tenantId: decodeURIComponent(parts[1]), tab };
  }
  return { name: "overview" };
}

export function useRoute(): Route {
  const [route, setRoute] = useState<Route>(() => parse(window.location.hash));
  useEffect(() => {
    const onHashChange = () => setRoute(parse(window.location.hash));
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);
  return route;
}

export function tenantUrl(tenantId: string, tab: Tab = "metrics"): string {
  return `#/tenants/${encodeURIComponent(tenantId)}/${tab}`;
}

export function newTenantUrl(): string {
  return "#/tenants/new";
}

export function navigate(hash: string): void {
  window.location.hash = hash;
}
