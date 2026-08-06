// Reads app/channels/chat.py's SSE stream. EventSource can't POST, so this
// is fetch + a ReadableStream reader with manual `\n\n` framing — the part
// of this widget most worth getting right, per plan §7.
//
// Server-side event types: token, acknowledgement, suggestions, handoff,
// actions, cards, final, error (app/brain/events.py's EventType). `_events()` also
// emits a bare `: ping` comment line as a heartbeat; any line that doesn't
// start with `data: ` (including that one) is simply not yielded here —
// that's what makes heartbeats a no-op for the reader rather than something
// it has to specially handle.

export type BrainEventType =
  | "token"
  | "acknowledgement"
  | "suggestions"
  | "handoff"
  | "actions"
  | "cards"
  | "final"
  | "error";

export interface BrainEvent {
  type: BrainEventType;
  text: string;
  tool?: string;
  data?: Record<string, unknown>;
}

export async function* readEventStream(response: Response): AsyncGenerator<BrainEvent> {
  if (!response.body) {
    return;
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) {
        break;
      }
      buffer += decoder.decode(value, { stream: true });

      let boundary = buffer.indexOf("\n\n");
      while (boundary !== -1) {
        const rawEvent = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);

        for (const line of rawEvent.split("\n")) {
          if (!line.startsWith("data: ")) {
            continue; // heartbeat comment lines, blank lines, etc.
          }
          const payload = line.slice("data: ".length);
          if (payload === "[DONE]") {
            return;
          }
          const parsed = tryParse(payload);
          if (parsed) {
            yield parsed;
          }
        }

        boundary = buffer.indexOf("\n\n");
      }
    }
  } finally {
    reader.releaseLock();
  }
}

function tryParse(payload: string): BrainEvent | null {
  try {
    const value = JSON.parse(payload) as Partial<BrainEvent>;
    if (typeof value.type !== "string") {
      return null;
    }
    return { type: value.type as BrainEventType, text: value.text ?? "", ...value };
  } catch {
    // A malformed frame is dropped, not fatal — the rest of the stream (and
    // the conversation) still has a chance to arrive intact.
    return null;
  }
}
