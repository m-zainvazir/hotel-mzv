import { useEffect, useState } from "preact/hooks";

import {
  ApiError,
  deleteVersion,
  listVersions,
  SessionInfo,
  switchToVersion,
  TenantVersionRow,
} from "../api";

/** The AI Prompt as it was in that version. `null` means the version was
 *  using the shared default template rather than its own override — which
 *  is itself worth showing, since reverting to it changes behaviour. */
function promptOf(version: TenantVersionRow): string | null {
  const override = version.config?.system_prompt_override;
  return typeof override === "string" && override.trim() ? override : null;
}

function excerpt(text: string, chars = 140): string {
  const flat = text.replace(/\s+/g, " ").trim();
  return flat.length > chars ? `${flat.slice(0, chars)}…` : flat;
}

type Pending = { id: string; action: "revert" | "delete" };

export function VersionsView({ tenantId, session }: { tenantId: string; session: SessionInfo }) {
  const isOperator = session.kind === "operator";
  const [versions, setVersions] = useState<TenantVersionRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [pending, setPending] = useState<Pending | null>(null);
  const [viewing, setViewing] = useState<TenantVersionRow | null>(null);

  function load(): void {
    setError(null);
    listVersions(tenantId)
      .then((body) => setVersions(body.versions))
      .catch((err) => setError(err instanceof ApiError ? err.message : "failed to load versions"));
  }

  useEffect(load, [tenantId]);

  async function run(action: "revert" | "delete", versionId: string): Promise<void> {
    setBusyId(versionId);
    setError(null);
    setPending(null);
    try {
      if (action === "revert") {
        await switchToVersion(tenantId, versionId);
      } else {
        await deleteVersion(tenantId, versionId);
      }
      load();
    } catch (err) {
      const fallback = action === "revert" ? "could not revert" : "could not delete version";
      setError(err instanceof ApiError ? err.message : fallback);
    } finally {
      setBusyId(null);
    }
  }

  if (error && !versions) {
    return <div class="admin-error-banner">{error}</div>;
  }
  if (!versions) {
    return <p class="admin-muted">Loading…</p>;
  }

  return (
    <div class="admin-section">
      <h2>Versions</h2>
      <p class="admin-muted">
        One entry per Deploy — never per save. Saves overwrite the single draft; Deploy is what
        publishes it here permanently.
      </p>
      <p class="admin-muted">
        Reverting restores that version's <strong>entire configuration</strong>, not just its AI
        Prompt — services, hours, buttons, flows and channels all go back to exactly how they were,
        so the bot behaves the way it did then. It doesn't burn a new version number.
      </p>

      {error && <div class="admin-error-banner">{error}</div>}
      {versions.length === 0 && (
        <p class="admin-muted">No deploys yet — this bot's first Deploy will create version 1.</p>
      )}

      {versions.map((version) => {
        const prompt = promptOf(version);
        const confirming = pending?.id === version.id ? pending.action : null;
        return (
          <div
            class={`admin-list-item${version.is_live ? " admin-version--live" : ""}`}
            key={version.id}
          >
            <div class="admin-list-item-header">
              <strong>
                version-{version.version_number}{" "}
                {version.is_live && <span class="admin-badge admin-badge--ok">Deployed</span>}
              </strong>
              {isOperator && !confirming && (
                <div class="admin-field-row" style={{ gap: "0.4rem" }}>
                  <button
                    class="admin-btn admin-btn--secondary"
                    title="View this version's AI Prompt"
                    onClick={() => setViewing(version)}
                  >
                    👁 View
                  </button>
                  {/* Reverting TO the deployed version is a no-op, so it's
                      offered only on the others. */}
                  {!version.is_live && (
                    <button
                      class="admin-btn admin-btn--secondary"
                      disabled={busyId === version.id}
                      onClick={() => setPending({ id: version.id, action: "revert" })}
                    >
                      ↺ Revert
                    </button>
                  )}
                  {/* The deployed version can't be deleted — the server
                      409s on it too, so this is a UI courtesy, not the
                      guard itself. */}
                  {!version.is_live && (
                    <button
                      class="admin-btn admin-btn--danger"
                      disabled={busyId === version.id}
                      onClick={() => setPending({ id: version.id, action: "delete" })}
                    >
                      🗑
                    </button>
                  )}
                </div>
              )}
            </div>

            <p class="admin-muted" style={{ margin: "0.25rem 0" }}>
              {new Date(version.deployed_at).toLocaleString()}
              {version.deployed_by ? ` · ${version.deployed_by}` : ""}
              {version.note ? ` · ${version.note}` : ""}
            </p>

            <p class="admin-version-excerpt">
              {prompt ? excerpt(prompt) : <em class="admin-muted">Using the shared default prompt</em>}
            </p>

            {confirming === "revert" && (
              <div class="admin-confirm">
                <span>
                  Make <strong>version-{version.version_number}</strong> the deployed one? The live
                  bot switches to its entire configuration on the very next message.
                </span>
                <div class="admin-field-row" style={{ gap: "0.4rem" }}>
                  <button class="admin-btn" disabled={busyId === version.id} onClick={() => run("revert", version.id)}>
                    {busyId === version.id ? "Reverting…" : "Yes, revert"}
                  </button>
                  <button class="admin-btn admin-btn--secondary" onClick={() => setPending(null)}>
                    Cancel
                  </button>
                </div>
              </div>
            )}

            {confirming === "delete" && (
              <div class="admin-confirm admin-confirm--danger">
                <span>
                  Delete <strong>version-{version.version_number}</strong> permanently? This can't
                  be undone, and you won't be able to revert to it afterwards.
                </span>
                <div class="admin-field-row" style={{ gap: "0.4rem" }}>
                  <button
                    class="admin-btn admin-btn--danger"
                    disabled={busyId === version.id}
                    onClick={() => run("delete", version.id)}
                  >
                    {busyId === version.id ? "Deleting…" : "Yes, delete"}
                  </button>
                  <button class="admin-btn admin-btn--secondary" onClick={() => setPending(null)}>
                    Cancel
                  </button>
                </div>
              </div>
            )}
          </div>
        );
      })}

      {viewing && (
        <div class="admin-modal-backdrop" onClick={() => setViewing(null)}>
          <div class="admin-modal" onClick={(e) => e.stopPropagation()}>
            <div class="admin-modal-header">
              <h2>
                version-{viewing.version_number}
                {viewing.is_live && <span class="admin-badge admin-badge--ok"> Deployed</span>}
              </h2>
              <button class="admin-modal-close" onClick={() => setViewing(null)} aria-label="Close">
                ✕
              </button>
            </div>
            <p class="admin-muted">
              {new Date(viewing.deployed_at).toLocaleString()}
              {viewing.deployed_by ? ` · ${viewing.deployed_by}` : ""}
            </p>
            <pre class="admin-version-prompt">
              {promptOf(viewing) ?? "This version used the shared default prompt template."}
            </pre>
          </div>
        </div>
      )}
    </div>
  );
}
