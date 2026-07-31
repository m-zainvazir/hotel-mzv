import { SessionInfo } from "../api";
import { Tab, tenantUrl } from "../router";
import { CallsView } from "./Calls";
import { ChatsView } from "./Chats";
import { ConfigView } from "./Config";
import { JobsEscalationsView } from "./JobsEscalations";
import { MetricsView } from "./Metrics";
import { SystemPromptView } from "./SystemPrompt";

const TABS: { tab: Tab; label: string }[] = [
  { tab: "metrics", label: "Metrics" },
  { tab: "config", label: "Config" },
  { tab: "prompt", label: "AI Prompt" },
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
  return (
    <div>
      <h1>{tenantId}</h1>
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
      {tab === "metrics" && <MetricsView tenantId={tenantId} />}
      {tab === "config" && <ConfigView tenantId={tenantId} session={session} />}
      {tab === "prompt" && <SystemPromptView tenantId={tenantId} />}
      {tab === "calls" && <CallsView tenantId={tenantId} />}
      {tab === "chats" && <ChatsView tenantId={tenantId} />}
      {(tab === "jobs" || tab === "escalations") && <JobsEscalationsView tenantId={tenantId} />}
    </div>
  );
}
