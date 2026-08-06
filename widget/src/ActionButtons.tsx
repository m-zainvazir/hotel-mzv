import type { ActionItem } from "./api";

export type { ActionItem };

export interface PickOptions {
  postback?: string;
  /** Send the turn without showing the visitor's text as a bubble. Used by
   *  the opening turn, whose prompt is machinery, not something anyone said. */
  silent?: boolean;
}

interface Props {
  items: ActionItem[];
  onPick: (value: string, options?: PickOptions) => void;
  /** Stacked, full-width rows (a flow node's buttons) vs. inline chips. */
  stacked?: boolean;
}

// The widget never resolves a slug or invents a URL — the server did that
// (app/flows/resolver.py) and this component renders only what arrived.
// Four behaviours, one row shape:
//
//   link    → a real <a>, opens in a new tab
//   flow    → sends the label as a message PLUS a `flow:<id>` postback, which
//             the server answers deterministically with no model involved
//   reply   → sends `value` (or the label) as an ordinary message; the model
//             answers it. The plain postback of a chat platform.
//   handoff → identical to reply, and deliberately so: the canned phrase is
//             what drives the existing `escalate` tool, exactly as a
//             quick-reply chip drives `book_job`.
export function ActionButtons({ items, onPick, stacked }: Props) {
  if (items.length === 0) {
    return null;
  }
  const wrapperClass = stacked ? "ai-recept-actions ai-recept-actions--stacked" : "ai-recept-chips";
  return (
    <div class={wrapperClass}>
      {items.map((item, index) => {
        const key = item.slug ?? `${item.type}-${index}`;
        if (item.type === "link" && item.url) {
          return (
            <a
              key={key}
              class="ai-recept-chip"
              href={item.url}
              target="_blank"
              rel="noopener noreferrer"
            >
              {item.label}
            </a>
          );
        }
        const options =
          item.type === "flow" && item.flow ? { postback: `flow:${item.flow}` } : undefined;
        return (
          <button
            key={key}
            type="button"
            class="ai-recept-chip"
            onClick={() => onPick(item.value || item.label, options)}
          >
            {item.label}
          </button>
        );
      })}
    </div>
  );
}
