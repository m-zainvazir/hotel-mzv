import { useRef, useState } from "preact/hooks";

import { ActionButtons, type PickOptions } from "./ActionButtons";
import type { CardItem } from "./api";

interface Props {
  items: CardItem[];
  onPick: (value: string, options?: PickOptions) => void;
}

// The generic-template carousel (Phase 9.2). Every URL here was already
// scheme- and host-checked server-side (app/flows/cards.py) — this file
// assumes nothing and validates nothing, which is deliberate: one validator
// in one place beats two that can disagree.
//
// Scroll-snap does the paging, so the arrows are a convenience over a
// gesture that already works; the track stays keyboard- and
// touch-scrollable if they're never clicked. `overflow-x` is on the track,
// never the panel, so a ten-card carousel can't widen the chat window.
export function Cards({ items, onPick }: Props) {
  const trackRef = useRef<HTMLDivElement | null>(null);
  const [index, setIndex] = useState(0);

  if (items.length === 0) {
    return null;
  }

  function scrollTo(next: number): void {
    const track = trackRef.current;
    if (!track) {
      return;
    }
    const clamped = Math.max(0, Math.min(items.length - 1, next));
    setIndex(clamped);
    const card = track.children[clamped] as HTMLElement | undefined;
    card?.scrollIntoView({ behavior: "smooth", block: "nearest", inline: "start" });
  }

  return (
    <div class="ai-recept-carousel">
      <div class="ai-recept-carousel-track" ref={trackRef}>
        {items.map((card, cardIndex) => (
          <div class="ai-recept-card" key={`${card.title}-${cardIndex}`}>
            {card.image_url && (
              <div class="ai-recept-card-image">
                <img
                  src={card.image_url}
                  alt=""
                  loading="lazy"
                  // The host page is the client's own site — don't leak
                  // their visitors' URLs to whatever CDN a scraped image
                  // happens to live on.
                  referrerpolicy="no-referrer"
                  // A dead image URL must cost the card its picture, not
                  // its content: hide the <img> and keep everything else.
                  onError={(event) => {
                    (event.currentTarget as HTMLImageElement).style.display = "none";
                  }}
                />
              </div>
            )}
            <div class="ai-recept-card-body">
              {card.url ? (
                <a
                  class="ai-recept-card-title"
                  href={card.url}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  {card.title}
                </a>
              ) : (
                <span class="ai-recept-card-title">{card.title}</span>
              )}
              {card.subtitle && <p class="ai-recept-card-subtitle">{card.subtitle}</p>}
              <ActionButtons items={card.buttons} onPick={onPick} stacked />
            </div>
          </div>
        ))}
      </div>

      {items.length > 1 && (
        <div class="ai-recept-carousel-nav">
          <button
            type="button"
            class="ai-recept-carousel-arrow"
            aria-label="Previous"
            disabled={index === 0}
            onClick={() => scrollTo(index - 1)}
          >
            ‹
          </button>
          <button
            type="button"
            class="ai-recept-carousel-arrow"
            aria-label="Next"
            disabled={index >= items.length - 1}
            onClick={() => scrollTo(index + 1)}
          >
            ›
          </button>
        </div>
      )}
    </div>
  );
}
