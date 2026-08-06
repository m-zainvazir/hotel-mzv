import { useEffect, useState } from "preact/hooks";

import { ApiError, getTenantConfig, saveTenantDraft, TenantDetail } from "../api";

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
  "${links}",
  "${flows}",
  "${cards_rule}",
];

// Phase 9.2 — the three placeholders that describe this bot's buttons,
// flows and cards to the model. A prompt written for another platform and
// pasted in here contains none of them, which would leave the model unaware
// its buttons exist. See app/brain/prompts/system.py::_augment.
const CATALOG_PLACEHOLDERS = ["${ui_rule}", "${links}", "${flows}"];

export function SystemPromptView({ tenantId }: { tenantId: string }) {
  const [detail, setDetail] = useState<TenantDetail | null>(null);
  const [text, setText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [savedNotice, setSavedNotice] = useState(false);

  function load(): void {
    setDetail(null);
    setError(null);
    getTenantConfig(tenantId)
      .then((d) => {
        setDetail(d);
        setText((d.config.system_prompt_override as string | null) || d._rendered_system_prompt || "");
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : "failed to load prompt"));
  }

  useEffect(load, [tenantId]);

  async function save(): Promise<void> {
    if (!detail) return;
    setSaving(true);
    setError(null);
    setSavedNotice(false);
    try {
      const saved = await saveTenantDraft(
        tenantId,
        { system_prompt_override: text || null },
        detail._draft_version,
      );
      setDetail(saved);
      setText(
        (saved.config.system_prompt_override as string | null) || saved._rendered_system_prompt || "",
      );
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
    if (!detail) return;
    setSaving(true);
    setError(null);
    setSavedNotice(false);
    try {
      const saved = await saveTenantDraft(
        tenantId,
        { system_prompt_override: null },
        detail._draft_version,
      );
      setDetail(saved);
      setText(saved._rendered_system_prompt || "");
      setSavedNotice(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : "reset failed");
    } finally {
      setSaving(false);
    }
  }

  function reloadDefaultIntoEditor(): void {
    if (!detail) return;
    setText(detail._rendered_system_prompt || "");
  }

  if (error && !detail) {
    return <div class="admin-error-banner">{error}</div>;
  }
  if (!detail) {
    return <p class="admin-muted">Loading…</p>;
  }

  const isCustom = Boolean(detail.config.system_prompt_override);
  const augmentation = (detail.config.prompt_augmentation as string) ?? "auto_append";
  // ${ui_rule} applies to every chat bot now, configured or not — so a
  // custom prompt missing these is always worth flagging.
  const missingCatalogTokens = CATALOG_PLACEHOLDERS.filter((token) => !text.includes(token));
  const showCatalogWarning = isCustom && missingCatalogTokens.length > 0;

  async function setAugmentation(mode: string): Promise<void> {
    if (!detail) return;
    setSaving(true);
    setError(null);
    try {
      const saved = await saveTenantDraft(
        tenantId,
        { prompt_augmentation: mode },
        detail._draft_version,
      );
      setDetail(saved);
      setSavedNotice(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : "save failed");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div>
      {error && <div class="admin-error-banner">{error}</div>}
      {savedNotice && (
        <div class="admin-tile" style={{ marginBottom: "1rem" }}>
          Saved as a draft — not live until you deploy it from the Config tab.
        </div>
      )}

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

        {showCatalogWarning && (
          <div class="admin-tile" style={{ margin: "0.75rem 0" }}>
            <strong>This prompt doesn't mention the bot's on-screen tools.</strong>
            <p class="admin-muted">
              It's missing {missingCatalogTokens.join(", ")} — the sections that tell the AI it
              can render buttons, quick replies and image cards at all, plus any buttons and
              flows you configured —{" "}
              {augmentation === "auto_append"
                ? "so they're being added automatically at the end. Paste a token in wherever you'd rather it appear and the automatic copy for that one stops."
                : "and automatic appending is turned off, so the AI currently can't see them and will answer in plain text. Paste the tokens in where you want them, or switch the setting below."}
            </p>
            <div class="admin-field">
              <label>If the prompt doesn't mention them</label>
              <select
                value={augmentation}
                disabled={saving}
                onChange={(e) => void setAugmentation((e.target as HTMLSelectElement).value)}
              >
                <option value="auto_append">Add them automatically at the end</option>
                <option value="placeholder_only">Leave my prompt exactly as written</option>
              </select>
            </div>
          </div>
        )}

        <textarea
          class="admin-prompt-editor"
          rows={28}
          value={text}
          onInput={(e) => setText((e.target as HTMLTextAreaElement).value)}
        />

        <div class="admin-field-row" style={{ marginTop: "0.75rem" }}>
          <button class="admin-btn" onClick={save} disabled={saving}>
            {saving ? "Saving…" : "Save draft"}
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
