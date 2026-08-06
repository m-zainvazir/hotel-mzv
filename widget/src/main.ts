// The frozen embed contract:
//
//   <script src="https://host/widget.js"
//           data-widget-key="pk_widget_hotelmzv_demo"
//           data-accent="#0f766e"></script>
//
// This is the one thing a client site pastes and can never be asked to
// re-paste — everything behind it (Preact, this file, the whole bundle) is
// replaceable at will. Keep this file's job narrow: read the attributes,
// mount into an isolated shadow root, get out of the way.

import { h, render } from "preact";

import { App } from "./App";
import cssText from "./styles.css?inline";

declare global {
  interface Window {
    __aiReceptionistWidgetMounted?: boolean;
  }
}

function currentScript(): HTMLScriptElement | null {
  // `document.currentScript` works for a plain, synchronously-executing
  // <script src="…">, which is exactly what the IIFE build + embed contract
  // above produces. Fall back to the last matching <script src> for the rare
  // case a host page injects it dynamically in a way that loses that.
  const script = document.currentScript as HTMLScriptElement | null;
  if (script) {
    return script;
  }
  const candidates = Array.from(
    document.querySelectorAll<HTMLScriptElement>("script[src*='widget.js']"),
  );
  return candidates[candidates.length - 1] ?? null;
}

function init(): void {
  // Idempotent: a page that includes the script tag twice (a common copy-
  // paste accident) must still end up with exactly one widget.
  if (window.__aiReceptionistWidgetMounted) {
    return;
  }
  window.__aiReceptionistWidgetMounted = true;

  const script = currentScript();
  if (!script) {
    console.error("[ai-receptionist] could not locate its own <script> tag");
    return;
  }

  const widgetKey = script.dataset.widgetKey;
  // Phase 9.1 — the Test Agent link path (`/test/{token}`, app/main.py).
  // Additive to the frozen `data-widget-key` contract: a Test Agent page
  // carries `data-test-token` instead and no widget key at all.
  const testToken = script.dataset.testToken;
  if (!widgetKey && !testToken) {
    console.error(
      "[ai-receptionist] missing required data-widget-key (or data-test-token) attribute",
    );
    return;
  }
  const autoOpen = script.dataset.autoOpen === "true";
  const accent = script.dataset.accent;
  const baseUrl = new URL(script.src, window.location.href).origin;

  const host = document.createElement("div");
  host.id = "ai-receptionist-widget-host";
  document.body.appendChild(host);

  // Shadow DOM, not an iframe or a plain div: it isolates the widget's CSS
  // from the host page's in both directions with none of an iframe's cost
  // (a second document, no shared JS context for streaming state).
  const shadow = host.attachShadow({ mode: "open" });

  const style = document.createElement("style");
  style.textContent = cssText;
  shadow.appendChild(style);

  const mountPoint = document.createElement("div");
  shadow.appendChild(mountPoint);

  render(h(App, { baseUrl, widgetKey: widgetKey ?? "", accent, testToken, autoOpen }), mountPoint);
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
