import { useEffect, useState } from "preact/hooks";

import { ApiError, createTestLink, getTenantConfig, SessionInfo } from "../api";
import { Tab, tenantUrl } from "../router";
import { CallsView } from "./Calls";
import { ChatsView } from "./Chats";
import { ConfigView } from "./Config";
import { DeployDialog } from "./DeployDialog";
import { JobsEscalationsView } from "./JobsEscalations";
import { KnowledgeView } from "./Knowledge";
import { MetricsView } from "./Metrics";
import { SystemPromptView } from "./SystemPrompt";
import { VersionsView } from "./Versions";

const TABS: { tab: Tab; label: string }[] = [
  { tab: "metrics", label: "Metrics" },
  { tab: "config", label: "Config" },
  { tab: "prompt", label: "AI Prompt" },
  { tab: "knowledge", label: "Knowledge" },
  { tab: "versions", label: "Versions" },
  { tab: "calls", label: "Calls" },
  { tab: "chats", label: "Chats" },
  { tab: "jobs", label: "Jobs & Escalations" },
];

export function TenantView({
  tenantId,
  tab,
  session,
}: {
  tenantId: string;
  tab: Tab;
  session: SessionInfo;
}) {
  // Deploying happens in exactly one place — the header's Deploy Agent
  // dialog — but the tab views each hold their own fetched copy of the
  // tenant detail. Without a nudge, deploying from the header left the
  // Config tab's "Draft — not live" banner on screen against a bot that no
  // longer had a draft. Bumping the key remounts the active tab, which
  // refetches; `headerKey` is the mirror image, so saving or discarding a
  // draft in a tab keeps the header's Deploy Agent • marker honest.
  const [tabKey, setTabKey] = useState(0);
  const [headerKey, setHeaderKey] = useState(0);

  return (
    <div>
      <div class="admin-tenant-header">
        <h1>{tenantId}</h1>
        <HeaderActions
          key={headerKey}
          tenantId={tenantId}
          onDeployed={() => setTabKey((n) => n + 1)}
        />
      </div>
      <nav class="admin-tabs">
        {TABS.map((entry) => (
          <a
            key={entry.tab}
            href={tenantUrl(tenantId, entry.tab)}
            class={tab === entry.tab || (entry.tab === "jobs" && tab === "escalations") ? "active" : ""}
          >
            {entry.label}
          </a>
        ))}
      </nav>
      <div key={tabKey}>
        {tab === "metrics" && <MetricsView tenantId={tenantId} />}
        {tab === "config" && (
          <ConfigView
            tenantId={tenantId}
            session={session}
            onDraftChanged={() => setHeaderKey((n) => n + 1)}
          />
        )}
        {tab === "prompt" && <SystemPromptView tenantId={tenantId} />}
        {tab === "knowledge" && <KnowledgeView tenantId={tenantId} />}
        {tab === "versions" && <VersionsView tenantId={tenantId} session={session} />}
        {tab === "calls" && <CallsView tenantId={tenantId} />}
        {tab === "chats" && <ChatsView tenantId={tenantId} />}
        {(tab === "jobs" || tab === "escalations") && <JobsEscalationsView tenantId={tenantId} />}
      </div>
    </div>
  );
}

function HeaderActions({ tenantId, onDeployed }: { tenantId: string; onDeployed?: () => void }) {
  // On every tab (it lives in the shared header, not a per-tab view) and
  // greyed per channel flag — 9.3's voice mode appears here with no
  // further UI work once it ships, by adding a second button/mode toggle.
  const [chatEnabled, setChatEnabled] = useState(true);
  const [hasDraft, setHasDraft] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [deployOpen, setDeployOpen] = useState(false);

  function refresh(): void {
    getTenantConfig(tenantId)
      .then((detail) => {
        const channels = detail.config.channels as { chat?: { enabled?: boolean } } | undefined;
        setChatEnabled(channels?.chat?.enabled !== false);
        setHasDraft(detail.has_draft);
      })
      .catch(() => setChatEnabled(true));
  }

  useEffect(() => {
    setChatEnabled(true);
    setHasDraft(false);
    refresh();
  }, [tenantId]);

  async function openTest(): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      // DRAFT, not live. "Test Agent" means "try what I'm working on" — the
      // published bot is what the share link is for. When there's no draft
      // the server falls back to live, so this is always "current state".
      const { url } = await createTestLink(tenantId, "chat", "draft");
      window.open(url, "_blank", "noopener");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "could not create a test link");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div class="admin-test-agent">
      {error && <span class="admin-field-error">{error}</span>}
      <button
        class="admin-btn admin-btn--secondary"
        onClick={openTest}
        disabled={busy || !chatEnabled}
        title={
          chatEnabled
            ? "Open a private preview of your unpublished draft"
            : "chat channel disabled"
        }
      >
        {busy ? "Opening…" : "✎ Test Agent"}
      </button>
      <button
        class="admin-btn"
        onClick={() => setDeployOpen(true)}
        title="Publish this bot and get its shareable link"
      >
        ▶ Deploy Agent{hasDraft ? " •" : ""}
      </button>
      {deployOpen && (
        <DeployDialog
          tenantId={tenantId}
          onClose={() => {
            setDeployOpen(false);
            refresh();
          }}
          onDeployed={() => {
            refresh();
            onDeployed?.();
          }}
        />
      )}
    </div>
  );
}
