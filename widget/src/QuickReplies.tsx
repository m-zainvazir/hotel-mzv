export interface QuickReplyItem {
  label: string;
  value: string;
}

interface Props {
  items: QuickReplyItem[];
  onPick: (value: string) => void;
}

// A clicked chip sends its human label as an ordinary user message — never
// a raw slot_start_iso. The model already holds the numbered list with the
// real ISO values in its own context (see check_availability's returned
// text), so book_job still gets a verbatim ISO. This component is a
// renderer only; the graph never learns a chip exists.
export function QuickReplies({ items, onPick }: Props) {
  if (items.length === 0) {
    return null;
  }
  return (
    <div class="ai-recept-chips">
      {items.map((item) => (
        <button
          key={item.value}
          type="button"
          class="ai-recept-chip"
          onClick={() => onPick(item.value)}
        >
          {item.label}
        </button>
      ))}
    </div>
  );
}
