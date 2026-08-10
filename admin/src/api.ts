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

export interface TenantVersionSummary {
  id: string;
  tenant_id: string;
  version_number: number;
  note: string;
  deployed_by: string;
  deployed_at: string;
  is_live: boolean;
}

export interface TenantVersionRow extends TenantVersionSummary {
  config: TenantConfig;
}

// Phase 9.1: GET/PUT/deploy/discard/switch all return this shape now —
// `config` is the draft when one exists, else live (the editor's working
// copy); `live_config` is always what's actually running. `_draft_version`
// is what PUT's `If-Match` targets now, not `_version` (the live row's own
// token, kept for reference/concurrent-deploy detection only).
export interface TenantDetail {
  config: TenantConfig;
  live_config: TenantConfig;
  has_draft: boolean;
  _draft_version: string | null;
  _version: string | null;
  _health?: ConfigHealth;
  _rendered_system_prompt?: string;
  /** The shared template with its ${placeholders} intact — what the AI Prompt
   *  editor pre-fills. Never pre-fill `_rendered_system_prompt`: saving that
   *  freezes `${local_time}` into a literal date. */
  _raw_system_prompt?: string;
  live_version: TenantVersionSummary | null;
  /** Permanent public link to this bot — always live config, never expires.
   *  Relative (`/bot/<key>`) when PUBLIC_BASE_URL isn't set; null when the
   *  tenant has no widget key. Use `absoluteShareUrl()` to render it. */
  share_url: string | null;
}

/** `share_url` resolved against this origin when the server returned a
 *  relative path (a dev box with no PUBLIC_BASE_URL). */
export function absoluteShareUrl(shareUrl: string | null | undefined): string | null {
  if (!shareUrl) return null;
  return shareUrl.startsWith("http") ? shareUrl : `${window.location.origin}${shareUrl}`;
}

export function getTenantConfig(tenantId: string): Promise<TenantDetail> {
  return request(`/tenants/${tenantId}`);
}

export function saveTenantDraft(
  tenantId: string,
  payload: Record<string, unknown>,
  draftVersion: string | null | undefined,
): Promise<TenantDetail> {
  const headers: Record<string, string> = {};
  if (draftVersion) headers["If-Match"] = draftVersion;
  return request(`/tenants/${tenantId}`, {
    method: "PUT",
    body: JSON.stringify(payload),
    headers,
  });
}

export function deployTenant(tenantId: string, note?: string): Promise<TenantDetail> {
  return request(`/tenants/${tenantId}/deploy`, {
    method: "POST",
    body: JSON.stringify({ note: note || "" }),
  });
}

export function discardDraft(tenantId: string): Promise<TenantDetail> {
  return request(`/tenants/${tenantId}/draft/discard`, { method: "POST" });
}

export function listVersions(tenantId: string): Promise<{ versions: TenantVersionRow[] }> {
  return request(`/tenants/${tenantId}/versions`);
}

export function switchToVersion(tenantId: string, versionId: string): Promise<TenantDetail> {
  return request(`/tenants/${tenantId}/versions/${versionId}/switch`, { method: "POST" });
}

export function deleteVersion(
  tenantId: string,
  versionId: string,
): Promise<{ deleted: string }> {
  return request(`/tenants/${tenantId}/versions/${versionId}/delete`, { method: "POST" });
}

// --- Test Agent link (Phase 9.1, shared with 9.3) ---------------------------

export function createTestLink(
  tenantId: string,
  mode: "chat" | "voice" = "chat",
  variant: "live" | "draft" = "live",
): Promise<{ url: string; expires_at: number }> {
  return request(`/tenants/${tenantId}/test-link`, {
    method: "POST",
    body: JSON.stringify({ mode, variant }),
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

// These three return a full TenantDetail (`{config, live_config, ...}`),
// NOT a bare TenantConfig — every lifecycle route ends in
// `_tenant_detail(...)` (app/channels/admin.py). They were typed as
// TenantConfig when Phase 9 Part B added them, which stayed true until
// Phase 9.1 wrapped the response; the annotation then quietly disabled the
// one thing that would have caught the difference, and `created.tenant_id`
// compiled fine while being `undefined` at runtime. Keep these accurate:
// `tsc --noEmit` in the build IS the regression guard here, since no Python
// test can see across the wire into TypeScript.
export function createTenant(payload: CreateTenantPayload): Promise<TenantDetail> {
  return request("/tenants", { method: "POST", body: JSON.stringify(payload) });
}

export function archiveTenant(tenantId: string): Promise<TenantDetail> {
  return request(`/tenants/${tenantId}/archive`, { method: "POST" });
}

export function restoreTenant(tenantId: string): Promise<TenantDetail> {
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
