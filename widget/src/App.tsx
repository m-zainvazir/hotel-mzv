import { useCallback, useEffect, useRef, useState } from "preact/hooks";

import { type ChatSessionResponse, sendMessage, startSession } from "./api";
import { type QuickReplyItem, QuickReplies } from "./QuickReplies";
import { type BrainEvent, readEventStream } from "./useStream";

interface HandoffInfo {
  destination?: string;
  transfer: boolean;
}

interface Message {
  id: string;
  role: "user" | "assistant";
  text: string;
  suggestions?: QuickReplyItem[];
  handoff?: HandoffInfo;
}

export interface AppProps {
  baseUrl: string;
  widgetKey: string;
  accent?: string;
}

let messageCounter = 0;
function nextId(): string {
  messageCounter += 1;
  return `m${Date.now()}-${messageCounter}`;
}

export function App({ baseUrl, widgetKey, accent }: AppProps) {
  const [open, setOpen] = useState(false);
  const [session, setSession] = useState<ChatSessionResponse | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [pending, setPending] = useState(false);
  const [input, setInput] = useState("");
  const [error, setError] = useState<string | null>(null);

  const abortRef = useRef<AbortController | null>(null);
  const listRef = useRef<HTMLDivElement | null>(null);
  const storageKey = `ai-receptionist-widget:${widgetKey}`;

  useEffect(() => () => abortRef.current?.abort(), []);

  useEffect(() => {
    if (listRef.current) {
      listRef.current.scrollTop = listRef.current.scrollHeight;
    }
  }, [messages, pending]);

  const ensureSession = useCallback(async (): Promise<ChatSessionResponse> => {
    if (session) {
      return session;
    }
    const cached = sessionStorage.getItem(storageKey);
    if (cached) {
      try {
        const parsed = JSON.parse(cached) as ChatSessionResponse;
        setSession(parsed);
        return parsed;
      } catch {
        sessionStorage.removeItem(storageKey);
      }
    }
    const fresh = await startSession(baseUrl, widgetKey);
    sessionStorage.setItem(storageKey, JSON.stringify(fresh));
    setSession(fresh);
    return fresh;
  }, [baseUrl, widgetKey, session, storageKey]);

  // Handshake happens on first open, not on page load — a visitor who never
  // clicks the launcher never costs a request.
  const openPanel = useCallback(async () => {
    setOpen(true);
    setError(null);
    try {
      await ensureSession();
    } catch {
      setError("Could not start a chat session. Please try again shortly.");
    }
  }, [ensureSession]);

  const applyEvent = useCallback((event: BrainEvent, assistantId: string) => {
    if (event.type === "token" || event.type === "acknowledgement") {
      if (!event.text) {
        return;
      }
      setMessages((prev) =>
        prev.map((m) => (m.id === assistantId ? { ...m, text: m.text + event.text } : m)),
      );
      return;
    }
    if (event.type === "suggestions") {
      const rawSlots = (event.data?.slots as { start_iso: string; label: string }[]) || [];
      const items: QuickReplyItem[] = rawSlots.map((slot) => ({
        label: slot.label,
        value: slot.label,
      }));
      setMessages((prev) =>
        prev.map((m) => (m.id === assistantId ? { ...m, suggestions: items } : m)),
      );
      return;
    }
    if (event.type === "handoff") {
      const destination = event.data?.destination as string | undefined;
      const transfer = Boolean(event.data?.transfer);
      setMessages((prev) =>
        prev.map((m) => (m.id === assistantId ? { ...m, handoff: { destination, transfer } } : m)),
      );
      return;
    }
    if (event.type === "error") {
      setMessages((prev) =>
        prev.map((m) =>
          m.id === assistantId && !m.text
            ? { ...m, text: "Sorry, I'm having trouble responding right now." }
            : m,
        ),
      );
    }
  }, []);

  const send = useCallback(
    async (rawText: string) => {
      const text = rawText.trim();
      if (!text || pending) {
        return;
      }
      setInput("");
      setError(null);

      let activeSession: ChatSessionResponse;
      try {
        activeSession = await ensureSession();
      } catch {
        setError("Could not start a chat session. Please try again shortly.");
        return;
      }

      const assistantId = nextId();
      setMessages((prev) => [
        ...prev,
        { id: nextId(), role: "user", text },
        { id: assistantId, role: "assistant", text: "" },
      ]);
      setPending(true);

      const controller = new AbortController();
      abortRef.current = controller;

      try {
        const response = await sendMessage(baseUrl, activeSession.token, text, controller.signal);
        if (!response.ok) {
          throw new Error(`chat request failed: ${response.status}`);
        }
        for await (const event of readEventStream(response)) {
          applyEvent(event, assistantId);
        }
      } catch (err) {
        if ((err as { name?: string }).name !== "AbortError") {
          setError("Sorry, something went wrong. Please try again.");
        }
      } finally {
        setPending(false);
        abortRef.current = null;
      }
    },
    [applyEvent, baseUrl, ensureSession, pending],
  );

  const accentStyle = accent
    ? ({ "--ai-recept-accent": accent } as unknown as Record<string, string>)
    : undefined;

  return (
    <div class="ai-recept-root" style={accentStyle}>
      {!open && (
        <button
          type="button"
          class="ai-recept-launcher"
          onClick={openPanel}
          aria-label={session?.tenant.launcher_label ?? "Chat with us"}
        >
          <span aria-hidden="true">💬</span>
        </button>
      )}

      {open && (
        <div class="ai-recept-panel" role="dialog" aria-label={session?.tenant.name ?? "Chat"}>
          <div class="ai-recept-header">
            <span>{session?.tenant.name ?? "Chat"}</span>
            <button
              type="button"
              class="ai-recept-close"
              onClick={() => setOpen(false)}
              aria-label="Close chat"
            >
              ✕
            </button>
          </div>

          <div class="ai-recept-messages" ref={listRef}>
            {session && messages.length === 0 && (
              <div class="ai-recept-bubble ai-recept-bubble--assistant">
                {session.tenant.greeting}
                {session.tenant.quick_replies && session.tenant.services.length > 0 && (
                  <QuickReplies
                    items={session.tenant.services.map((s) => ({ label: s.name, value: s.name }))}
                    onPick={send}
                  />
                )}
              </div>
            )}

            {messages.map((m, index) => {
              const isLastAssistant = m.role === "assistant" && index === messages.length - 1;
              const showTyping = pending && isLastAssistant && !m.text;
              return (
                <div key={m.id} class={`ai-recept-bubble ai-recept-bubble--${m.role}`}>
                  {showTyping ? <span class="ai-recept-typing">●●●</span> : m.text}
                  {m.suggestions && m.suggestions.length > 0 && (
                    <QuickReplies items={m.suggestions} onPick={send} />
                  )}
                  {m.handoff?.destination && (
                    <a class="ai-recept-callto" href={`tel:${m.handoff.destination}`}>
                      Call {m.handoff.destination}
                    </a>
                  )}
                </div>
              );
            })}

            {error && <div class="ai-recept-error">{error}</div>}
          </div>

          <form
            class="ai-recept-composer"
            onSubmit={(event) => {
              event.preventDefault();
              void send(input);
            }}
          >
            <input
              type="text"
              value={input}
              placeholder="Type a message…"
              disabled={pending}
              onInput={(event) => setInput((event.target as HTMLInputElement).value)}
            />
            <button type="submit" disabled={pending || !input.trim()} aria-label="Send">
              ➤
            </button>
          </form>
        </div>
      )}
    </div>
  );
}
