// The two calls behind the embed contract — POST /chat/session (handshake)
// and POST /chat (SSE). See app/channels/chat.py for the server side of
// both shapes; keep these interfaces in sync with its Pydantic models.

export interface ChatSessionService {
  slug: string;
  name: string;
  duration_minutes: number;
  price_usd: number | null;
}

// Phase 9.2 — one shape for every button, wherever it came from (the
// greeting menu, a flow node, offer_actions, or a card). The server has
// already resolved a catalog slug into this; the widget never looks a slug
// up itself and never sees a URL the model wrote unaided.
export interface ActionItem {
  type: "link" | "handoff" | "reply" | "flow";
  label: string;
  slug: string | null;
  url: string | null;
  value: string | null;
  flow: string | null;
}

export interface CardItem {
  title: string;
  subtitle: string;
  image_url: string | null;
  url: string | null;
  buttons: ActionItem[];
}

export interface ChatSessionTenant {
  name: string;
  greeting: string;
  accent_color: string;
  launcher_label: string;
  quick_replies: boolean;
  services: ChatSessionService[];
  // Empty unless the tenant set `chat.menu_flow` — then these render under
  // the greeting *instead of* the services chips.
  menu: ActionItem[];
  // When true the panel runs one real turn as it opens rather than showing
  // `greeting`, so the model writes its own opening message and buttons.
  opening_turn: boolean;
}

export interface ChatSessionResponse {
  session_id: string;
  token: string;
  expires_in: number;
  tenant: ChatSessionTenant;
}

export async function startSession(
  baseUrl: string,
  widgetKey: string,
): Promise<ChatSessionResponse> {
  const response = await fetch(`${baseUrl}/chat/session`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ widget_key: widgetKey }),
  });
  if (!response.ok) {
    throw new Error(`chat session handshake failed: ${response.status}`);
  }
  return (await response.json()) as ChatSessionResponse;
}

// Phase 9.1 — the Test Agent link's handshake. Same response shape as
// startSession above (`/chat/session`), but keyed by a signed test-link
// token instead of a public widget key — see app/main.py's `/test/session`
// and app/channels/test_links.py.
export async function startTestSession(
  baseUrl: string,
  testToken: string,
): Promise<ChatSessionResponse> {
  const response = await fetch(`${baseUrl}/test/session`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token: testToken }),
  });
  if (!response.ok) {
    throw new Error(`test session handshake failed: ${response.status}`);
  }
  return (await response.json()) as ChatSessionResponse;
}

// `postback` (Phase 9.2) is a clicked button's payload — today only
// `flow:<id>`. The server resolves it against this session's own tenant, so
// it's a routing hint, not a trust boundary; an unknown one just falls
// through to an ordinary model turn.
export function sendMessage(
  baseUrl: string,
  token: string,
  message: string,
  signal?: AbortSignal,
  postback?: string,
): Promise<Response> {
  return fetch(`${baseUrl}/chat`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify(postback ? { message, postback } : { message }),
    signal,
  });
}
