import { useEffect, useState } from "preact/hooks";

import { ApiError, ChatMessage, ChatSessionSummary, getChatMessages, listChats } from "../api";

export function ChatsView({ tenantId }: { tenantId: string }) {
  const [sessions, setSessions] = useState<ChatSessionSummary[] | null>(null);
  const [messages, setMessages] = useState<ChatMessage[] | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setMessages(null);
    setSelectedId(null);
    listChats(tenantId)
      .then((body) => setSessions(body.sessions))
      .catch((err) => setError(err instanceof ApiError ? err.message : "failed to load chats"));
  }, [tenantId]);

  async function openSession(sessionId: string) {
    try {
      const body = await getChatMessages(tenantId, sessionId);
      setSelectedId(sessionId);
      setMessages(body.messages);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "failed to load messages");
    }
  }

  return (
    <div>
      {error && <div class="admin-error-banner">{error}</div>}
      {sessions === null && !error && <p class="admin-muted">Loading…</p>}
      {sessions && sessions.length === 0 && <p class="admin-muted">No chat sessions yet.</p>}
      {sessions && sessions.length > 0 && (
        <table class="admin-table">
          <thead>
            <tr>
              <th>Started</th>
              <th>Origin</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {sessions.map((session) => (
              <tr key={session.id}>
                <td>{session.started_at}</td>
                <td>{session.origin ?? "—"}</td>
                <td>
                  <button
                    class="admin-btn admin-btn--secondary"
                    onClick={() => openSession(session.id)}
                  >
                    View transcript
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {selectedId && messages && (
        <div class="admin-section" style={{ marginTop: "1rem" }}>
          <h2>Transcript — {selectedId}</h2>
          <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem" }}>
            {messages.map((message) => (
              <div key={message.id}>
                <strong>{message.role}:</strong> {message.content}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
