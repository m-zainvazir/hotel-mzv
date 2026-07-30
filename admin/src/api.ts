// The client for /admin/api/* (app/channels/admin.py). Not an embed contract
// like widget/api.ts — this bundle is served same-origin from the app that
// hosts it, so relative paths always resolve correctly with no baseUrl
// plumbing to thread through every call.

const TOKEN_KEY = "ai-receptionist-admin-token";

export function getToken(): string | null {
  return sessionStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string): void {
  sessionStorage.setItem(TOKEN_KEY, token);
}

export function clearToken(): void {
  sessionStorage.removeItem(TOKEN_KEY);
}

export class AuthError extends Error {}

export class ApiError extends Error {
  status: number;
  detail: unknown;
  constructor(status: number, detail: unknown) {
    super(typeof detail === "string" ? detail : JSON.stringify(detail));
    this.status = status;
    this.detail = detail;
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = getToken();
  const headers = new Headers(init.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body) headers.set("Content-Type", "application/json");

  const response = await fetch(`/admin/api${path}`, { ...init, headers });

  if (response.status === 401) {
    clearToken();
    throw new AuthError("unauthorized — sign in again");
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: response.statusText }));
    throw new ApiError(response.status, body.detail ?? response.statusText);
  }
  if (response.status === 204 || response.headers.get("content-length") === "0") {
    return undefined as T;
  }
  return (await response.json()) as T;
}

// --- session -----------------------------------------------------------

export interface SessionInfo {
  kind: "operator" | "tenant";
  tenant_ids: string[] | null;
  capabilities: string[];
}

export function getSession(): Promise<SessionInfo> {
  return request("/session");
}

export function listTenantIds(): Promise<{ tenant_ids: string[] }> {
  return request("/tenants");
}

// --- overview / analytics ------------------------------------------------

export interface TenantMetrics {
  tenant_id: string;
  calls: number;
  call_seconds: number;
  cost_usd: number;
  jobs: number;
  escalations: number;
  chat_sessions: number;
  chat_messages: number;
}

export interface OverviewRow {
  tenant_id: string;
  name?: string;
  trade?: string;
  status?: string;
  metrics?: TenantMetrics;
  error?: string;
}

export function getOverview(): Promise<{ tenants: OverviewRow[] }> {
  return request("/overview");
}

export interface DailyMetric {
  day: string;
  calls: number;
  call_seconds: number;
  cost_usd: number;
  jobs: number;
  escalations: number;
  chat_sessions: number;
  chat_messages: number;
}

export function getTenantMetrics(
  tenantId: string,
  from?: string,
  to?: string,
): Promise<{ since: string; until: string; totals: TenantMetrics; daily: DailyMetric[] }> {
  const params = new URLSearchParams();
  if (from) params.set("from", from);
  if (to) params.set("to", to);
  const qs = params.toString();
  return request(`/tenants/${tenantId}/metrics${qs ? `?${qs}` : ""}`);
}

// --- tenant config ---------------------------------------------------------

// Deliberately `Record<string, unknown>`, not a rigid interface: the admin UI
// treats TenantConfig as "whatever the server currently holds", forwarding
// unknown fields back untouched on save — matching the server's own
// shallow-merge PUT semantics (app/channels/admin.py::put_tenant).
export type TenantConfig = Record<string, unknown> & {
  tenant_id: string;
  name: string;
  trade: string;
  timezone: string;
  status: string;
  greeting: string;
  persona: string;
  _health?: ConfigHealth;
  _version?: string | null;
};

export interface ConfigHealth {
  booking_provider: string;
  booking_is_live: boolean;
  notifications_provider: string;
  notifications_is_live: boolean;
  vapi_assistant_configured: boolean;
  chat_allowed_origins_empty: boolean;
  warm_transfer_enabled: boolean;
  mcp_servers_enabled: number;
}

export function getTenantConfig(tenantId: string): Promise<TenantConfig> {
  return request(`/tenants/${tenantId}`);
}

export function saveTenantConfig(
  tenantId: string,
  payload: Record<string, unknown>,
  version: string | null | undefined,
): Promise<TenantConfig> {
  const headers: Record<string, string> = {};
  if (version) headers["If-Match"] = version;
  return request(`/tenants/${tenantId}`, {
    method: "PUT",
    body: JSON.stringify(payload),
    headers,
  });
}

// --- calls / chats / jobs / escalations --------------------------------

export interface CallSummary {
  id: string;
  tenant_id: string;
  provider_call_id: string;
  from_number: string | null;
  to_number: string | null;
  started_at: string | null;
  ended_at: string | null;
  duration_seconds: number | null;
  ended_reason: string | null;
  cost_usd: number | null;
  channel: string;
  created_at: string;
}

export function listCalls(tenantId: string): Promise<{ calls: CallSummary[] }> {
  return request(`/tenants/${tenantId}/calls`);
}

export function getCall(tenantId: string, callId: string): Promise<Record<string, unknown>> {
  return request(`/tenants/${tenantId}/calls/${callId}`);
}

export interface ChatSessionSummary {
  id: string;
  tenant_id: string;
  widget_key: string;
  origin: string | null;
  started_at: string;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  created_at: string;
}

export function listChats(tenantId: string): Promise<{ sessions: ChatSessionSummary[] }> {
  return request(`/tenants/${tenantId}/chats`);
}

export function getChatMessages(
  tenantId: string,
  sessionId: string,
): Promise<{ session: ChatSessionSummary; messages: ChatMessage[] }> {
  return request(`/tenants/${tenantId}/chats/${sessionId}`);
}

export interface Escalation {
  id: string;
  reason: string;
  transferred_to: string;
  caller_summary: string;
  channel: string;
  created_at: string;
}

export function listEscalations(tenantId: string): Promise<{ escalations: Escalation[] }> {
  return request(`/tenants/${tenantId}/escalations`);
}

export interface JobRow {
  id: string;
  service_name: string;
  customer_name: string;
  scheduled_start: string;
  scheduled_end: string;
  status: string;
  channel: string;
}

export function listJobs(tenantId: string): Promise<{ jobs: JobRow[] }> {
  return request(`/tenants/${tenantId}/jobs`);
}
