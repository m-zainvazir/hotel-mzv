import { useEffect, useState } from "preact/hooks";

import {
  absoluteShareUrl,
  ApiError,
  deployTenant,
  getTenantConfig,
  TenantConfig,
  TenantDetail,
} from "../api";

// Publish targets. Only "Website" does anything today — it's the hosted
// share link above, which already works. The rest are declared now so the
// surface is visible and the wiring order is a product decision rather than
// a discovery: each becomes a real integration behind the same tile.
//
// Deliberately NOT disabled-and-greyed into invisibility: an operator
// should be able to see what this bot could be published to, and clicking
// one should say plainly that it isn't wired yet rather than doing nothing.
const PUBLISH_TARGETS: { id: string; label: string; icon: string; note: string }[] = [
  { id: "website", label: "Website", icon: "🌐", note: "" },
  { id: "whatsapp", label: "WhatsApp", icon: "💬", note: "Needs a Twilio WhatsApp sender." },
  { id: "messenger", label: "Messenger", icon: "📨", note: "Needs a Facebook page token." },
  { id: "instagram", label: "Instagram", icon: "📷", note: "Needs an Instagram business account." },
  { id: "telegram", label: "Telegram", icon: "✈️", note: "Needs a Telegram bot token." },
  { id: "sms", label: "SMS", icon: "📱", note: "Twilio is wired but not switched on." },
  { id: "slack", label: "Slack", icon: "#️⃣", note: "Needs a Slack app." },
  { id: "voice", label: "Voice Agent", icon: "📞", note: "Vapi — attach a phone number." },
  { id: "mcp", label: "Agent as MCP", icon: "🔌", note: "Expose this bot as an MCP server." },
  { id: "webhook", label: "Agent Webhook", icon: "🪝", note: "POST every turn to your endpoint." },
];

function changedSections(draft: TenantConfig, live: TenantConfig): string[] {
  const keys = new Set([...Object.keys(draft), ...Object.keys(live)]);
  const changed: string[] = [];
  for (const key of keys) {
    if (JSON.stringify(draft[key]) !== JSON.stringify(live[key])) changed.push(key);
  }
  return changed.sort();
}

export function DeployDialog({
  tenantId,
  onClose,
  onDeployed,
}: {
  tenantId: string;
  onClose: () => void;
  onDeployed: () => void;
}) {
  const [detail, setDetail] = useState<TenantDetail | null>(null);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [deployed, setDeployed] = useState(false);
  const [copied, setCopied] = useState(false);
  const [target, setTarget] = useState<string | null>(null);

  useEffect(() => {
    getTenantConfig(tenantId)
      .then(setDetail)
      .catch((err) => setError(err instanceof ApiError ? err.message : "could not load this bot"));
  }, [tenantId]);

  async function doDeploy(): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      const updated = await deployTenant(tenantId, note);
      setDetail(updated);
      setDeployed(true);
      setNote("");
      onDeployed();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "deploy failed");
    } finally {
      setBusy(false);
    }
  }

  const shareUrl = absoluteShareUrl(detail?.share_url);

  async function copyShareUrl(): Promise<void> {
    if (!shareUrl) return;
    try {
      await navigator.clipboard.writeText(shareUrl);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard needs a secure context; the input is selectable anyway.
      setError("Couldn't copy automatically — select the link and copy it.");
    }
  }

  const diff = detail?.has_draft ? changedSections(detail.config, detail.live_config) : [];

  return (
    <div class="admin-modal-backdrop" onClick={onClose}>
      <div class="admin-modal" onClick={(e) => e.stopPropagation()}>
        <div class="admin-modal-header">
          <h2>Publish agent to connected platforms</h2>
          <button class="admin-modal-close" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </div>

        {error && <div class="admin-error-banner">{error}</div>}
        {!detail && !error && <p class="admin-muted">Loading…</p>}

        {detail && (
          <>
            <div class="admin-modal-row">
              <div>
                <strong>
                  {detail.has_draft
                    ? "Are you sure you want to deploy changes?"
                    : "Nothing to deploy"}
                </strong>
                <p class="admin-muted">
                  {detail.has_draft
                    ? "This publishes your draft — the live bot answers with it on the very next message."
                    : "There are no unpublished changes. Edit and save a draft first."}
                </p>
                {diff.length > 0 && (
                  <p class="admin-muted">
                    Changed: {diff.map((k) => <code key={k}>{k} </code>)}
                  </p>
                )}
                {deployed && <p class="admin-badge admin-badge--ok">Deployed</p>}
              </div>
              <button class="admin-btn" onClick={doDeploy} disabled={busy || !detail.has_draft}>
                {busy ? "Deploying…" : "▶ Deploy agent"}
              </button>
            </div>

            {detail.has_draft && (
              <div class="admin-field">
                <label>Note (optional — shows in the Versions tab)</label>
                <input
                  type="text"
                  value={note}
                  onInput={(e) => setNote((e.target as HTMLInputElement).value)}
                />
              </div>
            )}

            <div class="admin-modal-block">
              <h3>Share agent</h3>
              <p class="admin-muted">
                A permanent public link to the <strong>live</strong> bot. Unlike a Test Agent
                link it never expires and doesn't follow your draft — safe to send to anyone.
              </p>
              {shareUrl ? (
                <div class="admin-share-row">
                  <input type="text" readOnly value={shareUrl} onClick={(e) => (e.target as HTMLInputElement).select()} />
                  <button class="admin-btn admin-btn--secondary" onClick={copyShareUrl}>
                    {copied ? "Copied" : "Copy"}
                  </button>
                </div>
              ) : (
                <p class="admin-muted">
                  This bot has no widget key yet, so there's nothing to share. Add one in Config.
                </p>
              )}
            </div>

            <div class="admin-modal-block">
              <h3>Publish agent</h3>
              <div class="admin-publish-grid">
                {PUBLISH_TARGETS.map((t) => (
                  <button
                    key={t.id}
                    class={`admin-publish-tile${target === t.id ? " admin-publish-tile--active" : ""}`}
                    onClick={() => setTarget(t.id)}
                  >
                    <span class="admin-publish-icon" aria-hidden="true">
                      {t.icon}
                    </span>
                    <span>{t.label}</span>
                  </button>
                ))}
              </div>
              {target && (
                <p class="admin-muted">
                  {target === "website" ? (
                    <>
                      <strong>Website</strong> — already live. Use the share link above, or embed
                      the widget on your own site with the <code>&lt;script&gt;</code> tag from{" "}
                      <code>widget/README.md</code>.
                    </>
                  ) : (
                    <>
                      <strong>{PUBLISH_TARGETS.find((t) => t.id === target)?.label}</strong> — not
                      connected yet. {PUBLISH_TARGETS.find((t) => t.id === target)?.note}
                    </>
                  )}
                </p>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
