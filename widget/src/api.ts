// The two calls behind the embed contract — POST /chat/session (handshake)
// and POST /chat (SSE). See app/channels/chat.py for the server side of
// both shapes; keep these interfaces in sync with its Pydantic models.

export interface ChatSessionService {
  slug: string;
  name: string;
  duration_minutes: number;
  price_usd: number | null;
}

export interface ChatSessionTenant {
  name: string;
  greeting: string;
  accent_color: string;
  launcher_label: string;
  quick_replies: boolean;
  services: ChatSessionService[];
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

export function sendMessage(
  baseUrl: string,
  token: string,
  message: string,
  signal?: AbortSignal,
): Promise<Response> {
  return fetch(`${baseUrl}/chat`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify({ message }),
    signal,
  });
}
