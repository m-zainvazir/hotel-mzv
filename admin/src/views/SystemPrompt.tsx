import { useEffect, useState } from "preact/hooks";

import { ApiError, getTenantConfig, saveTenantConfig, TenantConfig } from "../api";

// Mirrors app/brain/prompts/system.py::render_system_prompt's substitute()
// call — shown so an admin editing the override knows which tokens still
// resolve live if left in place.
const PLACEHOLDERS = [
  "${business_name}",
  "${trade}",
  "${persona}",
  "${channel}",
  "${local_time}",
  "${timezone}",
  "${business_hours}",
  "${services}",
  "${length_rule}",
  "${safety_rules}",
];

export function SystemPromptView({ tenantId }: { tenantId: string }) {
  const [config, setConfig] = useState<TenantConfig | null>(null);
  const [text, setText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [savedNotice, setSavedNotice] = useState(false);

  function load(): void {
    setConfig(null);
    setError(null);
    getTenantConfig(tenantId)
      .then((c) => {
        setConfig(c);
        setText((c.system_prompt_override as string | null) || c._rendered_system_prompt || "");
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : "failed to load prompt"));
  }

  useEffect(load, [tenantId]);

  async function save(): Promise<void> {
    if (!config) return;
    setSaving(true);
    setError(null);
    setSavedNotice(false);
    try {
      const saved = await saveTenantConfig(
        tenantId,
        { system_prompt_override: text || null },
        config._version,
      );
      setConfig(saved);
      setText((saved.system_prompt_override as string | null) || saved._rendered_system_prompt || "");
      setSavedNotice(true);
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        setError(`${err.message} — reloading the current version.`);
        load();
      } else {
        setError(err instanceof Error ? err.message : "save failed");
      }
    } finally {
      setSaving(false);
    }
  }

  async function clearOverride(): Promise<void> {
    if (!config) return;
    setSaving(true);
    setError(null);
    setSavedNotice(false);
    try {
      const saved = await saveTenantConfig(tenantId, { system_prompt_override: null }, config._version);
      setConfig(saved);
      setText(saved._rendered_system_prompt || "");
      setSavedNotice(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : "reset failed");
    } finally {
      setSaving(false);
    }
  }

  function reloadDefaultIntoEditor(): void {
    if (!config) return;
    setText(config._rendered_system_prompt || "");
  }

  if (error && !config) {
    return <div class="admin-error-banner">{error}</div>;
  }
  if (!config) {
    return <p class="admin-muted">Loading…</p>;
  }

  const isCustom = Boolean(config.system_prompt_override);

  return (
    <div>
      {error && <div class="admin-error-banner">{error}</div>}
      {savedNotice && <div class="admin-tile" style={{ marginBottom: "1rem" }}>Saved.</div>}

      <div class="admin-section">
        <h2>AI Prompt</h2>
        <p class="admin-muted">
          This is the complete text sent to the model as instructions for how the bot should behave —
          persona, hours, services, booking rules, safety handling, all in one place. It's organised
          into sections with <code>##</code> headings below, same as the file it's based on.
        </p>
        <p class="admin-muted">
          Status:{" "}
          <span class={`admin-badge ${isCustom ? "admin-badge--ok" : "admin-badge--warn"}`}>
            {isCustom ? "custom override active" : "using shared default template"}
          </span>
        </p>
        <p class="admin-muted">
          Tokens like <code>${"{business_name}"}</code> below are filled in live from this tenant's own
          config every turn — leave them in place to keep that section dynamic, or replace them with
          fixed text. Available tokens: {PLACEHOLDERS.join(", ")}.
        </p>

        <textarea
          class="admin-prompt-editor"
          rows={28}
          value={text}
          onInput={(e) => setText((e.target as HTMLTextAreaElement).value)}
        />

        <div class="admin-field-row" style={{ marginTop: "0.75rem" }}>
          <button class="admin-btn" onClick={save} disabled={saving}>
            {saving ? "Saving…" : "Save changes"}
          </button>
          <button class="admin-btn admin-btn--secondary" onClick={reloadDefaultIntoEditor} disabled={saving}>
            Reload current default into editor
          </button>
          <button class="admin-btn admin-btn--danger" onClick={clearOverride} disabled={saving || !isCustom}>
            Clear override (use shared default)
          </button>
        </div>
      </div>
    </div>
  );
}
