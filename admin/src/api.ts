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

export interface TenantSummary {
  tenant_id: string;
  name: string;
  status: string;
}

export function listTenantIds(): Promise<{ tenant_ids: string[]; tenants: TenantSummary[] }> {
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
  system_prompt_override?: string | null;
  _health?: ConfigHealth;
  _rendered_system_prompt?: string;
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

// --- bot lifecycle (Phase 9 Part B) -----------------------------------------

export type CreateTenantMode = "blank" | "template" | "clone";

export interface CreateTenantPayload {
  mode: CreateTenantMode;
  template?: string;
  source_tenant_id?: string;
  tenant_id: string;
  name: string;
  trade: string;
  greeting: string;
  escalation_phone: string;
}

export function createTenant(payload: CreateTenantPayload): Promise<TenantConfig> {
  return request("/tenants", { method: "POST", body: JSON.stringify(payload) });
}

export function archiveTenant(tenantId: string): Promise<TenantConfig> {
  return request(`/tenants/${tenantId}/archive`, { method: "POST" });
}

export function restoreTenant(tenantId: string): Promise<TenantConfig> {
  return request(`/tenants/${tenantId}/restore`, { method: "POST" });
}

export function purgeTenant(
  tenantId: string,
  confirmTenantId: string,
): Promise<{ tenant_id: string; deleted: Record<string, number> }> {
  return request(`/tenants/${tenantId}/purge`, {
    method: "POST",
    body: JSON.stringify({ tenant_id: confirmTenantId }),
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

// --- knowledge base / RAG (Phase 9 Part C) ----------------------------------

export interface KnowledgeDocument {
  id: string;
  tenant_id: string;
  title: string;
  source_type: "text" | "file" | "url";
  source_ref: string;
  status: "pending" | "indexing" | "ready" | "failed";
  error: string | null;
  chunk_count: number;
  bytes: number;
  created_at: string;
  indexed_at: string | null;
}

export interface KnowledgeHit {
  chunk_id: string;
  document_id: string;
  document_title: string;
  content: string;
  similarity: number;
}

export function listKnowledge(tenantId: string): Promise<{ documents: KnowledgeDocument[] }> {
  return request(`/tenants/${tenantId}/knowledge`);
}

export function addKnowledgeText(
  tenantId: string,
  title: string,
  text: string,
): Promise<KnowledgeDocument> {
  return request(`/tenants/${tenantId}/knowledge/text`, {
    method: "POST",
    body: JSON.stringify({ title, text }),
  });
}

export async function uploadKnowledgeFiles(
  tenantId: string,
  files: File[],
): Promise<{ documents: (KnowledgeDocument | { title: string; status: string; error: string })[] }> {
  const form = new FormData();
  for (const file of files) form.append("files", file);
  const token = getToken();
  const headers = new Headers();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  // No Content-Type here — the browser sets the multipart boundary itself;
  // setting it manually (as `request()`'s JSON path does) breaks the upload.
  const response = await fetch(`/admin/api/tenants/${tenantId}/knowledge/upload`, {
    method: "POST",
    headers,
    body: form,
  });
  if (response.status === 401) {
    clearToken();
    throw new AuthError("unauthorized — sign in again");
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: response.statusText }));
    throw new ApiError(response.status, body.detail ?? response.statusText);
  }
  return response.json();
}

export function addKnowledgeUrl(
  tenantId: string,
  url: string,
  crawl: boolean,
): Promise<{ documents: KnowledgeDocument[] }> {
  return request(`/tenants/${tenantId}/knowledge/url`, {
    method: "POST",
    body: JSON.stringify({ url, crawl }),
  });
}

export function reindexKnowledgeDocument(
  tenantId: string,
  documentId: string,
): Promise<KnowledgeDocument> {
  return request(`/tenants/${tenantId}/knowledge/${documentId}/reindex`, { method: "POST" });
}

export function deleteKnowledgeDocument(
  tenantId: string,
  documentId: string,
): Promise<{ deleted: string }> {
  return request(`/tenants/${tenantId}/knowledge/${documentId}/delete`, { method: "POST" });
}

export function searchKnowledgePreview(
  tenantId: string,
  query: string,
): Promise<{ hits: KnowledgeHit[] }> {
  return request(`/tenants/${tenantId}/knowledge/search`, {
    method: "POST",
    body: JSON.stringify({ query }),
  });
}
