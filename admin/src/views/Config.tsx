import { useEffect, useState } from "preact/hooks";

import {
  ApiError,
  archiveTenant,
  createTestLink,
  discardDraft,
  getTenantConfig,
  purgeTenant,
  restoreTenant,
  saveTenantDraft,
  SessionInfo,
  TenantConfig,
  TenantDetail,
} from "../api";
import { navigate } from "../router";

const WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"];

interface DayHoursValue {
  open: string;
  close: string;
}

interface ServiceValue {
  slug: string;
  name: string;
  description: string;
  duration_minutes: number;
  price_usd: number | null;
  emergency: boolean;
  aliases: string[];
  event_type_id: number | null;
}

interface EmergencyValue {
  escalation_phone: string;
  keywords: string[];
  holding_message: string;
  alert_template: string;
  allow_warm_transfer: boolean;
}

interface BookingValue {
  provider: string;
  calendar_id: string | null;
  event_type_id: number | null;
  require_address: boolean;
  slot_granularity_minutes: number;
  lead_time_hours: number;
  horizon_days: number;
  max_slots_returned: number;
}

interface NotificationsValue {
  provider: string;
  from_number: string | null;
  confirmation_template: string;
}

interface VoiceValue {
  provider: string;
  voice_id: string | null;
  model: string;
  speed: number;
}

interface ChatValue {
  allowed_origins: string[];
  accent_color: string;
  launcher_label: string;
  quick_replies: boolean;
  greeting: string | null;
  menu_flow: string | null;
}

interface KnowledgeValue {
  enabled: boolean;
  top_k: number;
  min_similarity: number;
  max_chunks: number;
}

interface McpServerValue {
  name: string;
  enabled: boolean;
  transport: string;
  url: string | null;
  tool_allowlist: string[];
  auth_secret_ref: string | null;
}

interface LinkValue {
  slug: string;
  label: string;
  url: string | null;
  value: string | null;
  flow: string | null;
  description: string;
  type: "link" | "handoff" | "reply" | "flow";
}

interface FlowValue {
  id: string;
  say: string;
  buttons: string[];
  description: string;
}

interface UiValue {
  buttons: boolean;
  cards: boolean;
  allowed_hosts: string[];
  max_cards: number;
  opening_turn: boolean;
}

const UI_DEFAULTS: UiValue = {
  buttons: true,
  cards: true,
  allowed_hosts: [],
  max_cards: 10,
  opening_turn: true,
};

interface ChannelToggleValue {
  enabled: boolean;
}

interface ChannelsValue {
  chat: ChannelToggleValue;
  voice: ChannelToggleValue;
}

type FieldErrors = Map<string, string>;

function parseFieldErrors(detail: unknown): FieldErrors {
  const errors: FieldErrors = new Map();
  if (Array.isArray(detail)) {
    for (const item of detail) {
      if (item && Array.isArray(item.loc)) {
        errors.set(item.loc.join("."), String(item.msg ?? "invalid"));
      }
    }
  }
  return errors;
}

function diffTopLevelKeys(draft: TenantConfig, live: TenantConfig): string[] {
  // A flat key walk, deliberately — no diff library. Good enough to tell an
  // operator WHICH sections changed before they deploy; the section forms
  // themselves are the place to see exactly what.
  const keys = new Set([...Object.keys(draft), ...Object.keys(live)]);
  const changed: string[] = [];
  for (const key of keys) {
    if (JSON.stringify(draft[key]) !== JSON.stringify(live[key])) changed.push(key);
  }
  return changed.sort();
}

export function ConfigView({
  tenantId,
  session,
  onDraftChanged,
}: {
  tenantId: string;
  session: SessionInfo;
  //: Saving or discarding a draft here changes what the header's "Deploy
  //: Agent" button should say (it carries a • when a draft exists). The
  //: header holds its own fetched copy, so it has to be told.
  onDraftChanged?: () => void;
}) {
  const isOperator = session.kind === "operator";
  const [detail, setDetail] = useState<TenantDetail | null>(null);
  const [config, setConfig] = useState<TenantConfig | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<FieldErrors>(new Map());
  const [saving, setSaving] = useState(false);
  const [savedNotice, setSavedNotice] = useState(false);

  function load(): void {
    setConfig(null);
    setDetail(null);
    setError(null);
    getTenantConfig(tenantId)
      .then((d) => {
        setDetail(d);
        setConfig(d.config);
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : "failed to load config"));
  }

  useEffect(load, [tenantId]);

  async function save(): Promise<void> {
    if (!config || !detail) return;
    setSaving(true);
    setError(null);
    setFieldErrors(new Map());
    setSavedNotice(false);
    try {
      const saved = await saveTenantDraft(tenantId, config, detail._draft_version);
      setDetail(saved);
      setConfig(saved.config);
      setSavedNotice(true);
      onDraftChanged?.();
    } catch (err) {
      if (err instanceof ApiError && err.status === 422) {
        setFieldErrors(parseFieldErrors(err.detail));
        setError("Fix the highlighted fields and save again.");
      } else if (err instanceof ApiError && err.status === 409) {
        setError(`${err.message} — reloading the current version.`);
        load();
      } else if (err instanceof ApiError && err.status === 403) {
        setError(`Not permitted: ${err.message}`);
      } else {
        setError(err instanceof Error ? err.message : "save failed");
      }
    } finally {
      setSaving(false);
    }
  }

  // Deploying deliberately does NOT live here. There is exactly one publish
  // path — the header's "Deploy Agent" dialog — because two of them meant two
  // independently-fetched copies of `has_draft`: deploying from the header
  // left this tab's "Draft — not live" banner on screen against a bot that no
  // longer had a draft, which reads as a failed deploy.
  async function previewDraft(): Promise<void> {
    setError(null);
    try {
      const { url } = await createTestLink(tenantId, "chat", "draft");
      window.open(url, "_blank", "noopener");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "could not create a test link");
    }
  }

  async function doDiscard(): Promise<void> {
    setSaving(true);
    setError(null);
    try {
      const updated = await discardDraft(tenantId);
      setDetail(updated);
      setConfig(updated.config);
      setSavedNotice(false);
      onDraftChanged?.();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "could not discard draft");
    } finally {
      setSaving(false);
    }
  }

  function update<K extends keyof TenantConfig>(key: K, value: TenantConfig[K]): void {
    if (!config) return;
    setConfig({ ...config, [key]: value });
  }

  if (error && !config) {
    return <div class="admin-error-banner">{error}</div>;
  }
  if (!config || !detail) {
    return <p class="admin-muted">Loading…</p>;
  }

  const health = detail._health;
  const diffKeys = detail.has_draft ? diffTopLevelKeys(detail.config, detail.live_config) : [];

  return (
    <div>
      {detail.has_draft && (
        <div class="admin-section admin-section--draft-banner">
          <strong>Draft — not live.</strong>{" "}
          {diffKeys.length} section{diffKeys.length === 1 ? "" : "s"} changed since the live
          version{diffKeys.length > 0 && `: ${diffKeys.join(", ")}`}. Publish it with{" "}
          <strong>▶ Deploy Agent</strong> at the top of this page.
          <div class="admin-field-row" style={{ marginTop: "0.5rem" }}>
            <button
              class="admin-btn admin-btn--secondary"
              onClick={previewDraft}
              disabled={saving}
              title="Chat with the bot exactly as this draft would behave — before publishing it"
            >
              Preview draft
            </button>
            <button class="admin-btn admin-btn--secondary" onClick={doDiscard} disabled={saving}>
              Discard draft
            </button>
          </div>
        </div>
      )}

      {health && (
        <div class="admin-tiles" style={{ marginBottom: "1rem" }}>
          <HealthBadge label="Booking" live={health.booking_is_live} value={health.booking_provider} />
          <HealthBadge
            label="Notifications"
            live={health.notifications_is_live}
            value={health.notifications_provider}
          />
          <HealthBadge label="Vapi assistant" live={health.vapi_assistant_configured} value="" />
          <HealthBadge label="Warm transfer" live={health.warm_transfer_enabled} value="" />
          <HealthBadge label="Widget origin lock" live={!health.chat_allowed_origins_empty} value="" />
        </div>
      )}

      {error && <div class="admin-error-banner">{error}</div>}
      {savedNotice && (
        <div class="admin-tile" style={{ marginBottom: "1rem" }}>
          Saved as a draft — not live until you publish it with ▶ Deploy Agent at the top of
          this page.
        </div>
      )}

      <div class="admin-section">
        <h2>Basics</h2>
        <p class="admin-muted">tenant_id: {config.tenant_id} (fixed)</p>
        <div class="admin-field-row">
          <TextField label="Name" value={config.name} onChange={(v) => update("name", v)} />
          <TextField label="Trade" value={config.trade} onChange={(v) => update("trade", v)} />
          <TextField
            label="Timezone (IANA)"
            value={config.timezone}
            onChange={(v) => update("timezone", v)}
            error={fieldErrors.get("timezone")}
          />
        </div>
        <TextAreaField label="Greeting" value={config.greeting} onChange={(v) => update("greeting", v)} />
        <TextAreaField label="Persona" value={config.persona} onChange={(v) => update("persona", v)} />
        <div class="admin-field">
          <label>Status {!isOperator && "(operator only)"}</label>
          <select
            value={config.status as string}
            disabled={!isOperator}
            onChange={(e) => update("status", (e.target as HTMLSelectElement).value)}
          >
            <option value="active">active</option>
            <option value="paused">paused</option>
            <option value="onboarding">onboarding</option>
            <option value="archived">archived</option>
          </select>
          <span class="admin-muted" style={{ fontSize: "0.8rem" }}>
            The Danger Zone below is the one-click way to archive/restore — same effect as
            picking a value here and saving.
          </span>
        </div>
        <ListField
          label="Phone numbers"
          disabled={!isOperator}
          values={(config.phone_numbers as string[]) ?? []}
          onChange={(v) => update("phone_numbers", v as never)}
        />
        <ListField
          label="Widget keys"
          disabled={!isOperator}
          values={(config.widget_keys as string[]) ?? []}
          onChange={(v) => update("widget_keys", v as never)}
        />
      </div>

      <HoursSection
        value={(config.hours as Record<string, DayHoursValue | null>) ?? {}}
        onChange={(v) => update("hours", v as never)}
        errors={fieldErrors}
      />

      <ServicesSection
        value={(config.services as ServiceValue[]) ?? []}
        onChange={(v) => update("services", v as never)}
        errors={fieldErrors}
      />

      <EmergencySection
        value={config.emergency as unknown as EmergencyValue}
        onChange={(v) => update("emergency", v as never)}
        errors={fieldErrors}
      />

      <BookingSection
        value={config.booking as unknown as BookingValue}
        onChange={(v) => update("booking", v as never)}
        isOperator={isOperator}
        errors={fieldErrors}
      />

      <NotificationsSection
        value={config.notifications as unknown as NotificationsValue}
        onChange={(v) => update("notifications", v as never)}
      />

      <VoiceSection
        value={config.voice as unknown as VoiceValue}
        onChange={(v) => update("voice", v as never)}
        isOperator={isOperator}
        errors={fieldErrors}
      />

      <ChatSection
        value={config.chat as unknown as ChatValue}
        onChange={(v) => update("chat", v as never)}
      />

      <KnowledgeSection
        value={config.knowledge as unknown as KnowledgeValue}
        onChange={(v) => update("knowledge", v as never)}
        errors={fieldErrors}
      />

      <McpServersSection
        value={(config.mcp_servers as McpServerValue[]) ?? []}
        onChange={(v) => update("mcp_servers", v as never)}
        isOperator={isOperator}
        errors={fieldErrors}
      />

      <LinksSection
        value={(config.links as LinkValue[]) ?? []}
        onChange={(v) => update("links", v as never)}
        flows={(config.flows as FlowValue[]) ?? []}
        errors={fieldErrors}
      />

      <FlowsSection
        value={(config.flows as FlowValue[]) ?? []}
        onChange={(v) => update("flows", v as never)}
        links={(config.links as LinkValue[]) ?? []}
        menuFlow={(config.chat as unknown as ChatValue)?.menu_flow ?? null}
        onMenuFlowChange={(id) =>
          update("chat", {
            ...(config.chat as unknown as ChatValue),
            menu_flow: id,
          } as never)
        }
        errors={fieldErrors}
      />

      <UiSection
        value={(config.ui as unknown as UiValue) ?? UI_DEFAULTS}
        onChange={(v) => update("ui", v as never)}
        errors={fieldErrors}
      />

      <ChannelsSection
        value={config.channels as unknown as ChannelsValue}
        onChange={(v) => update("channels", v as never)}
      />

      <div class="admin-section">
        <h2>Vapi wiring (read-only — change via `provision_vapi`, not here)</h2>
        <pre style={{ fontSize: "0.8rem" }}>{JSON.stringify(config.vapi, null, 2)}</pre>
      </div>

      <button class="admin-btn" onClick={save} disabled={saving}>
        {saving ? "Saving…" : "Save draft"}
      </button>

      {/* DangerZone reads the LIVE status, never the effective
          (draft-preferred) one. Archive and Restore act on the live row
          immediately via `set_tenant_status` — deliberately, since a
          lifecycle change isn't a config edit and shouldn't wait for a
          deploy. Passing `config.status` meant that on any bot with an
          unpublished draft, archiving succeeded but the panel kept showing
          "Archive" and left Purge greyed out forever, because the draft
          still said "active". The two disagree the moment a draft exists. */}
      {isOperator && (
        <DangerZone
          tenantId={tenantId}
          status={detail.live_config.status as string}
          hasDraft={detail.has_draft}
          onChanged={load}
        />
      )}
    </div>
  );
}

function DangerZone({
  tenantId,
  status,
  hasDraft,
  onChanged,
}: {
  /** The LIVE status — see the call site. */
  tenantId: string;
  status: string;
  hasDraft: boolean;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirmText, setConfirmText] = useState("");

  const isArchived = status === "archived";

  async function doArchive(): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      await archiveTenant(tenantId);
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not archive");
    } finally {
      setBusy(false);
    }
  }

  async function doRestore(): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      await restoreTenant(tenantId);
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not restore");
    } finally {
      setBusy(false);
    }
  }

  async function doPurge(): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      await purgeTenant(tenantId, confirmText);
      navigate("#/");
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not purge");
      setBusy(false);
    }
  }

  return (
    <div class="admin-section admin-section--danger">
      <h2>Danger Zone</h2>

      {error && <div class="admin-error-banner">{error}</div>}

      <div class="admin-danger-row">
        <div>
          <strong>{isArchived ? "Restore this bot" : "Archive this bot"}</strong>
          <p class="admin-muted">
            {isArchived
              ? "Restoring flips status back to active — it answers on voice and chat again immediately."
              : "Archiving stops this bot answering on voice and chat. Every row (jobs, calls, transcripts) is kept — this is reversible."}
          </p>
          {/* Archive/Restore skip the draft entirely, so an operator who
              also has unsaved config edits isn't left wondering why the
              status here doesn't match the Status field above. */}
          {hasDraft && (
            <p class="admin-muted">
              This takes effect immediately on the live bot — it doesn't wait for a Deploy, and
              your unpublished draft is left untouched.
            </p>
          )}
        </div>
        <button
          class="admin-btn admin-btn--secondary"
          disabled={busy}
          onClick={isArchived ? doRestore : doArchive}
        >
          {isArchived ? "Restore" : "Archive"}
        </button>
      </div>

      <div class="admin-danger-row">
        <div>
          <strong>Purge this bot</strong>
          <p class="admin-muted">
            Irreversibly deletes every row this bot has — jobs, calls, chat transcripts,
            escalations, everything. Only possible once archived. Type the tenant id (
            <code>{tenantId}</code>) to confirm.
          </p>
          <input
            type="text"
            value={confirmText}
            disabled={!isArchived || busy}
            placeholder={tenantId}
            onInput={(e) => setConfirmText((e.target as HTMLInputElement).value)}
          />
        </div>
        <button
          class="admin-btn admin-btn--danger"
          disabled={!isArchived || busy || confirmText !== tenantId}
          onClick={doPurge}
        >
          Purge permanently
        </button>
      </div>
    </div>
  );
}

function HealthBadge({ label, live, value }: { label: string; live: boolean; value: string }) {
  return (
    <div class="admin-tile">
      <div class="admin-tile-label">{label}</div>
      <div>
        <span class={`admin-badge ${live ? "admin-badge--ok" : "admin-badge--warn"}`}>
          {value || (live ? "on" : "off")}
        </span>
      </div>
    </div>
  );
}

function TextField({
  label,
  value,
  onChange,
  error,
  disabled,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  error?: string;
  disabled?: boolean;
}) {
  return (
    <div class="admin-field">
      <label>{label}</label>
      <input
        type="text"
        value={value}
        disabled={disabled}
        onInput={(e) => onChange((e.target as HTMLInputElement).value)}
      />
      {error && <span class="admin-field-error">{error}</span>}
    </div>
  );
}

function TextAreaField({
  label,
  value,
  onChange,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <div class="admin-field">
      <label>{label}</label>
      <textarea value={value} onInput={(e) => onChange((e.target as HTMLTextAreaElement).value)} />
    </div>
  );
}

function NumberField({
  label,
  value,
  onChange,
  error,
  disabled,
}: {
  label: string;
  value: number;
  onChange: (v: number) => void;
  error?: string;
  disabled?: boolean;
}) {
  return (
    <div class="admin-field">
      <label>{label}</label>
      <input
        type="number"
        value={value}
        disabled={disabled}
        onInput={(e) => onChange(Number((e.target as HTMLInputElement).value))}
      />
      {error && <span class="admin-field-error">{error}</span>}
    </div>
  );
}

function CheckboxField({
  label,
  checked,
  onChange,
  disabled,
}: {
  label: string;
  checked: boolean;
  onChange: (v: boolean) => void;
  disabled?: boolean;
}) {
  return (
    <label class="admin-checkbox">
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange((e.target as HTMLInputElement).checked)}
      />
      {label}
    </label>
  );
}

function ListField({
  label,
  values,
  onChange,
  disabled,
}: {
  label: string;
  values: string[];
  onChange: (v: string[]) => void;
  disabled?: boolean;
}) {
  // Holds the RAW text, not `values.join(", ")`.
  //
  // This was a controlled input whose displayed value was re-derived from
  // the parsed array on every keystroke — so typing a comma produced
  // ["a", ""], `filter(Boolean)` dropped the empty entry, and the field
  // re-rendered as "a" with the comma erased. A trailing space went the
  // same way. The net effect: a "(comma-separated)" field that physically
  // could not accept a second item. It affects every list in this panel —
  // emergency keywords, service aliases, allowed origins, MCP tool
  // allowlists — not just the one it was reported on.
  const [text, setText] = useState(values.join(", "));
  const external = values.join(" ");

  // Re-sync only when the incoming array genuinely differs from what the
  // current text parses to — i.e. the config was replaced from outside
  // (tenant switch, load, discard draft, revert). Mid-typing, "a," parses
  // to ["a"] which still matches, so the keystroke survives.
  useEffect(() => {
    const local = parseList(text).join(" ");
    if (local !== external) {
      setText(values.join(", "));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [external]);

  return (
    <div class="admin-field">
      <label>{label} (comma-separated)</label>
      <input
        type="text"
        value={text}
        disabled={disabled}
        onInput={(e) => {
          const raw = (e.target as HTMLInputElement).value;
          setText(raw);
          onChange(parseList(raw));
        }}
      />
    </div>
  );
}

function parseList(raw: string): string[] {
  return raw
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

function HoursSection({
  value,
  onChange,
  errors,
}: {
  value: Record<string, DayHoursValue | null>;
  onChange: (v: Record<string, DayHoursValue | null>) => void;
  errors: FieldErrors;
}) {
  return (
    <div class="admin-section">
      <h2>Hours</h2>
      {errors.get("hours") && <div class="admin-field-error">{errors.get("hours")}</div>}
      {WEEKDAYS.map((day) => {
        const hours = value[day];
        const closed = hours === null || hours === undefined;
        return (
          <div class="admin-day-row" key={day}>
            <span>{day.slice(0, 3)}</span>
            <input
              type="time"
              value={hours?.open ?? "09:00"}
              disabled={closed}
              onInput={(e) =>
                onChange({
                  ...value,
                  [day]: { open: (e.target as HTMLInputElement).value, close: hours?.close ?? "17:00" },
                })
              }
            />
            <input
              type="time"
              value={hours?.close ?? "17:00"}
              disabled={closed}
              onInput={(e) =>
                onChange({
                  ...value,
                  [day]: { open: hours?.open ?? "09:00", close: (e.target as HTMLInputElement).value },
                })
              }
            />
            <CheckboxField
              label="Closed"
              checked={closed}
              onChange={(isClosed) =>
                onChange({ ...value, [day]: isClosed ? null : { open: "09:00", close: "17:00" } })
              }
            />
          </div>
        );
      })}
    </div>
  );
}

function ServicesSection({
  value,
  onChange,
  errors,
}: {
  value: ServiceValue[];
  onChange: (v: ServiceValue[]) => void;
  errors: FieldErrors;
}) {
  function update(index: number, patch: Partial<ServiceValue>): void {
    onChange(value.map((s, i) => (i === index ? { ...s, ...patch } : s)));
  }
  function remove(index: number): void {
    onChange(value.filter((_, i) => i !== index));
  }
  function add(): void {
    onChange([
      ...value,
      {
        slug: "",
        name: "",
        description: "",
        duration_minutes: 60,
        price_usd: null,
        emergency: false,
        aliases: [],
        event_type_id: null,
      },
    ]);
  }

  return (
    <div class="admin-section">
      <h2>Services</h2>
      {value.map((service, index) => (
        <div class="admin-list-item" key={index}>
          <div class="admin-list-item-header">
            <strong>{service.name || service.slug || `Service ${index + 1}`}</strong>
            <button class="admin-btn admin-btn--danger" onClick={() => remove(index)}>
              Remove
            </button>
          </div>
          <div class="admin-field-row">
            <TextField
              label="Slug"
              value={service.slug}
              onChange={(v) => update(index, { slug: v })}
              error={errors.get(`services.${index}.slug`)}
            />
            <TextField label="Name" value={service.name} onChange={(v) => update(index, { name: v })} />
            <NumberField
              label="Duration (min)"
              value={service.duration_minutes}
              onChange={(v) => update(index, { duration_minutes: v })}
              error={errors.get(`services.${index}.duration_minutes`)}
            />
          </div>
          <div class="admin-field-row">
            <TextField
              label="Description"
              value={service.description}
              onChange={(v) => update(index, { description: v })}
            />
            <NumberField
              label="Price (USD, 0 = unset)"
              value={service.price_usd ?? 0}
              onChange={(v) => update(index, { price_usd: v || null })}
            />
            <CheckboxField
              label="Emergency service"
              checked={service.emergency}
              onChange={(v) => update(index, { emergency: v })}
            />
          </div>
          <ListField
            label="Aliases"
            values={service.aliases}
            onChange={(v) => update(index, { aliases: v })}
          />
        </div>
      ))}
      <button class="admin-btn admin-btn--secondary" onClick={add}>
        Add service
      </button>
    </div>
  );
}

function EmergencySection({
  value,
  onChange,
  errors,
}: {
  value: EmergencyValue;
  onChange: (v: EmergencyValue) => void;
  errors: FieldErrors;
}) {
  return (
    <div class="admin-section">
      <h2>Emergency</h2>
      <div class="admin-field-row">
        <TextField
          label="Escalation phone"
          value={value.escalation_phone}
          onChange={(v) => onChange({ ...value, escalation_phone: v })}
          error={errors.get("emergency.escalation_phone")}
        />
        <CheckboxField
          label="Allow warm transfer (voice)"
          checked={value.allow_warm_transfer}
          onChange={(v) => onChange({ ...value, allow_warm_transfer: v })}
        />
      </div>
      <ListField
        label="Keywords"
        values={value.keywords}
        onChange={(v) => onChange({ ...value, keywords: v })}
      />
      <TextAreaField
        label="Holding message"
        value={value.holding_message}
        onChange={(v) => onChange({ ...value, holding_message: v })}
      />
      <TextAreaField
        label="Alert template"
        value={value.alert_template}
        onChange={(v) => onChange({ ...value, alert_template: v })}
      />
    </div>
  );
}

function BookingSection({
  value,
  onChange,
  isOperator,
  errors,
}: {
  value: BookingValue;
  onChange: (v: BookingValue) => void;
  isOperator: boolean;
  errors: FieldErrors;
}) {
  return (
    <div class="admin-section">
      <h2>Booking</h2>
      <p class="admin-muted">
        Once provider is "calcom", Cal.com's own event-type schedule governs availability — hours/
        lead-time/granularity below become prompt copy only.
      </p>
      <div class="admin-field-row">
        <div class="admin-field">
          <label>Provider</label>
          <select
            value={value.provider}
            onChange={(e) => onChange({ ...value, provider: (e.target as HTMLSelectElement).value })}
          >
            <option value="stub">stub</option>
            <option value="google">google</option>
            <option value="calcom">calcom</option>
            <option value="mcp_calcom">mcp_calcom (Cal.com via its hosted MCP server)</option>
          </select>
        </div>
        <NumberField
          label="Cal.com event_type_id (operator only)"
          value={value.event_type_id ?? 0}
          disabled={!isOperator}
          onChange={(v) => onChange({ ...value, event_type_id: v || null })}
          error={errors.get("booking.event_type_id")}
        />
        <CheckboxField
          label="Require address"
          checked={value.require_address}
          onChange={(v) => onChange({ ...value, require_address: v })}
        />
      </div>
      <div class="admin-field-row">
        <NumberField
          label="Slot granularity (min)"
          value={value.slot_granularity_minutes}
          onChange={(v) => onChange({ ...value, slot_granularity_minutes: v })}
        />
        <NumberField
          label="Lead time (hours)"
          value={value.lead_time_hours}
          onChange={(v) => onChange({ ...value, lead_time_hours: v })}
        />
        <NumberField
          label="Horizon (days)"
          value={value.horizon_days}
          onChange={(v) => onChange({ ...value, horizon_days: v })}
        />
        <NumberField
          label="Max slots returned"
          value={value.max_slots_returned}
          onChange={(v) => onChange({ ...value, max_slots_returned: v })}
        />
      </div>
    </div>
  );
}

function NotificationsSection({
  value,
  onChange,
}: {
  value: NotificationsValue;
  onChange: (v: NotificationsValue) => void;
}) {
  return (
    <div class="admin-section">
      <h2>Notifications</h2>
      <div class="admin-field-row">
        <div class="admin-field">
          <label>Provider</label>
          <select
            value={value.provider}
            onChange={(e) => onChange({ ...value, provider: (e.target as HTMLSelectElement).value })}
          >
            <option value="stub">stub</option>
            <option value="twilio">twilio</option>
          </select>
        </div>
        <TextField
          label="From number"
          value={value.from_number ?? ""}
          onChange={(v) => onChange({ ...value, from_number: v || null })}
        />
      </div>
      <TextAreaField
        label="Confirmation template"
        value={value.confirmation_template}
        onChange={(v) => onChange({ ...value, confirmation_template: v })}
      />
    </div>
  );
}

function VoiceSection({
  value,
  onChange,
  isOperator,
  errors,
}: {
  value: VoiceValue;
  onChange: (v: VoiceValue) => void;
  isOperator: boolean;
  errors: FieldErrors;
}) {
  return (
    <div class="admin-section">
      <h2>Voice</h2>
      <div class="admin-field-row">
        <TextField label="Provider" value={value.provider} onChange={(v) => onChange({ ...value, provider: v })} />
        <TextField
          label="voice_id (operator only — see CLAUDE.md consent rule)"
          value={value.voice_id ?? ""}
          disabled={!isOperator}
          onChange={(v) => onChange({ ...value, voice_id: v || null })}
          error={errors.get("voice.voice_id")}
        />
        <TextField label="Model" value={value.model} onChange={(v) => onChange({ ...value, model: v })} />
        <NumberField
          label="Speed"
          value={value.speed}
          onChange={(v) => onChange({ ...value, speed: v })}
          error={errors.get("voice.speed")}
        />
      </div>
    </div>
  );
}

function ChatSection({ value, onChange }: { value: ChatValue; onChange: (v: ChatValue) => void }) {
  return (
    <div class="admin-section">
      <h2>Chat widget</h2>
      <ListField
        label="Allowed origins (empty = any)"
        values={value.allowed_origins}
        onChange={(v) => onChange({ ...value, allowed_origins: v })}
      />
      <div class="admin-field-row">
        <TextField
          label="Accent color"
          value={value.accent_color}
          onChange={(v) => onChange({ ...value, accent_color: v })}
        />
        <TextField
          label="Launcher label"
          value={value.launcher_label}
          onChange={(v) => onChange({ ...value, launcher_label: v })}
        />
        <CheckboxField
          label="Quick replies"
          checked={value.quick_replies}
          onChange={(v) => onChange({ ...value, quick_replies: v })}
        />
      </div>
    </div>
  );
}

function KnowledgeSection({
  value,
  onChange,
  errors,
}: {
  value: KnowledgeValue;
  onChange: (v: KnowledgeValue) => void;
  errors: FieldErrors;
}) {
  return (
    <div class="admin-section">
      <h2>Knowledge base</h2>
      <p class="admin-muted">
        Document search (see the Knowledge tab) only reaches conversations when this is on —
        uploading and searching documents there works either way, but the bot itself won't use
        them until "Enabled" is checked here and saved.
      </p>
      <CheckboxField
        label="Enabled"
        checked={value.enabled}
        onChange={(v) => onChange({ ...value, enabled: v })}
      />
      <div class="admin-field-row">
        <NumberField
          label="Top K"
          value={value.top_k}
          onChange={(v) => onChange({ ...value, top_k: v })}
          error={errors.get("knowledge.top_k")}
        />
        <NumberField
          label="Min similarity"
          value={value.min_similarity}
          onChange={(v) => onChange({ ...value, min_similarity: v })}
          error={errors.get("knowledge.min_similarity")}
        />
        <NumberField
          label="Max chunks"
          value={value.max_chunks}
          onChange={(v) => onChange({ ...value, max_chunks: v })}
          error={errors.get("knowledge.max_chunks")}
        />
      </div>
    </div>
  );
}

function McpServersSection({
  value,
  onChange,
  isOperator,
  errors,
}: {
  value: McpServerValue[];
  onChange: (v: McpServerValue[]) => void;
  isOperator: boolean;
  errors: FieldErrors;
}) {
  function update(index: number, patch: Partial<McpServerValue>): void {
    onChange(value.map((s, i) => (i === index ? { ...s, ...patch } : s)));
  }
  function remove(index: number): void {
    onChange(value.filter((_, i) => i !== index));
  }
  function add(): void {
    onChange([
      ...value,
      {
        name: "",
        enabled: true,
        transport: "http",
        url: "",
        tool_allowlist: [],
        auth_secret_ref: null,
      },
    ]);
  }

  return (
    <div class="admin-section">
      <h2>MCP servers {!isOperator && "(operator only)"}</h2>
      <p class="admin-muted">
        A tenant-submitted server URL is an SSRF vector once this becomes tenant-editable —
        operator-only for that reason (plans/phase10.md item 12).
      </p>
      {value.map((server, index) => (
        <div class="admin-list-item" key={index}>
          <div class="admin-list-item-header">
            <strong>{server.name || `Server ${index + 1}`}</strong>
            <button class="admin-btn admin-btn--danger" disabled={!isOperator} onClick={() => remove(index)}>
              Remove
            </button>
          </div>
          <div class="admin-field-row">
            <TextField
              label="Name"
              value={server.name}
              disabled={!isOperator}
              onChange={(v) => update(index, { name: v })}
              error={errors.get(`mcp_servers.${index}.name`)}
            />
            <TextField
              label="URL"
              value={server.url ?? ""}
              disabled={!isOperator}
              onChange={(v) => update(index, { url: v })}
            />
            <CheckboxField
              label="Enabled"
              checked={server.enabled}
              disabled={!isOperator}
              onChange={(v) => update(index, { enabled: v })}
            />
          </div>
        </div>
      ))}
      <button class="admin-btn admin-btn--secondary" disabled={!isOperator} onClick={add}>
        Add MCP server
      </button>
    </div>
  );
}

function LinksSection({
  value,
  onChange,
  flows,
  errors,
}: {
  value: LinkValue[];
  onChange: (v: LinkValue[]) => void;
  flows: FlowValue[];
  errors: FieldErrors;
}) {
  function update(index: number, patch: Partial<LinkValue>): void {
    onChange(value.map((l, i) => (i === index ? { ...l, ...patch } : l)));
  }
  function remove(index: number): void {
    onChange(value.filter((_, i) => i !== index));
  }
  function add(): void {
    onChange([
      ...value,
      { slug: "", label: "", url: "", value: null, flow: null, description: "", type: "link" },
    ]);
  }

  return (
    <div class="admin-section">
      <h2>Buttons</h2>
      <p class="admin-muted">
        One catalog, four places it renders: the greeting menu, a flow's buttons, a card's
        buttons, and whatever the model picks when it calls <code>offer_actions</code>. The model
        only ever names a slug from here — never a URL it made up. Chat-only (a voice caller
        can't click a button).
      </p>
      <ul class="admin-muted" style={{ marginTop: "-0.25rem" }}>
        <li>
          <strong>link</strong> — opens a URL in a new tab.
        </li>
        <li>
          <strong>flow</strong> — jumps straight to a scripted flow below, with{" "}
          <em>no AI request at all</em>. Use this for Main Menu and anything that must always say
          the same thing.
        </li>
        <li>
          <strong>reply</strong> — sends text back as if the visitor typed it, so the AI answers.
          Use it where the answer genuinely needs thinking.
        </li>
        <li>
          <strong>handoff</strong> — same as reply, but the canned phrase is what makes the bot
          escalate to a human.
        </li>
      </ul>
      {value.map((link, index) => (
        <div class="admin-list-item" key={index}>
          <div class="admin-list-item-header">
            <strong>{link.label || link.slug || `Button ${index + 1}`}</strong>
            <button class="admin-btn admin-btn--danger" onClick={() => remove(index)}>
              Remove
            </button>
          </div>
          <div class="admin-field-row">
            <TextField
              label="Slug"
              value={link.slug}
              onChange={(v) => update(index, { slug: v })}
              error={errors.get(`links.${index}.slug`)}
            />
            <TextField label="Label" value={link.label} onChange={(v) => update(index, { label: v })} />
            <div class="admin-field">
              <label>Type</label>
              <select
                value={link.type}
                onChange={(e) =>
                  update(index, { type: (e.target as HTMLSelectElement).value as LinkValue["type"] })
                }
              >
                <option value="link">link — open a URL</option>
                <option value="flow">flow — jump to a scripted step (no AI)</option>
                <option value="reply">reply — send text back to the AI</option>
                <option value="handoff">handoff — reach a human</option>
              </select>
            </div>
          </div>
          <div class="admin-field-row">
            {link.type === "link" && (
              <TextField
                label="URL"
                value={link.url ?? ""}
                onChange={(v) => update(index, { url: v })}
                error={errors.get(`links.${index}.url`)}
              />
            )}
            {link.type === "flow" && (
              <div class="admin-field">
                <label>Goes to flow</label>
                {/* A select over the tenant's own flows, so a dangling
                    reference is unpickable rather than merely 422-able. */}
                <select
                  value={link.flow ?? ""}
                  onChange={(e) =>
                    update(index, { flow: (e.target as HTMLSelectElement).value || null })
                  }
                >
                  <option value="">— pick a flow —</option>
                  {flows.map((flow) => (
                    <option key={flow.id} value={flow.id}>
                      {flow.id}
                    </option>
                  ))}
                </select>
                {errors.get(`links.${index}.flow`) && (
                  <span class="admin-field-error">{errors.get(`links.${index}.flow`)}</span>
                )}
              </div>
            )}
            {(link.type === "reply" || link.type === "handoff") && (
              <TextField
                label="Sends this text (blank = the label)"
                value={link.value ?? ""}
                onChange={(v) => update(index, { value: v || null })}
              />
            )}
            <TextField
              label="Description (what the model reads to decide)"
              value={link.description}
              onChange={(v) => update(index, { description: v })}
            />
          </div>
        </div>
      ))}
      <button class="admin-btn admin-btn--secondary" onClick={add}>
        Add button
      </button>
    </div>
  );
}

function FlowsSection({
  value,
  onChange,
  links,
  menuFlow,
  onMenuFlowChange,
  errors,
}: {
  value: FlowValue[];
  onChange: (v: FlowValue[]) => void;
  links: LinkValue[];
  menuFlow: string | null;
  onMenuFlowChange: (id: string | null) => void;
  errors: FieldErrors;
}) {
  function update(index: number, patch: Partial<FlowValue>): void {
    onChange(value.map((f, i) => (i === index ? { ...f, ...patch } : f)));
  }
  function remove(index: number): void {
    onChange(value.filter((_, i) => i !== index));
  }
  function add(): void {
    onChange([...value, { id: "", say: "", buttons: [], description: "" }]);
  }
  function toggleButton(index: number, slug: string, on: boolean): void {
    const current = value[index].buttons;
    update(index, {
      buttons: on ? [...current, slug] : current.filter((s) => s !== slug),
    });
  }
  function moveButton(index: number, at: number, by: number): void {
    const next = [...value[index].buttons];
    const to = at + by;
    if (to < 0 || to >= next.length) {
      return;
    }
    [next[at], next[to]] = [next[to], next[at]];
    update(index, { buttons: next });
  }

  return (
    <div class="admin-section">
      <h2>Flows</h2>
      <p class="admin-muted">
        A flow is one scripted step: fixed wording plus buttons. Clicking a flow button shows it{" "}
        <strong>exactly as written, with no AI involved</strong> — that's what makes a Main Menu
        button always behave the same way. The AI can also jump into one on its own when someone
        types something that matches (it reads the description below to decide).
      </p>
      <p class="admin-muted">
        A flow can't ask for or store anything — for that, add a <code>reply</code> button and let
        the AI take over.
      </p>

      <div class="admin-field">
        <label>Menu shown under the greeting</label>
        <select
          value={menuFlow ?? ""}
          onChange={(e) => onMenuFlowChange((e.target as HTMLSelectElement).value || null)}
        >
          <option value="">— none (show service chips instead) —</option>
          {value.map((flow) => (
            <option key={flow.id} value={flow.id}>
              {flow.id}
            </option>
          ))}
        </select>
        <span class="admin-muted">
          This flow's buttons appear before the visitor types anything. Point your "Main Menu"
          button at the same flow so the two can never drift apart.
        </span>
        {errors.get("chat.menu_flow") && (
          <span class="admin-field-error">{errors.get("chat.menu_flow")}</span>
        )}
      </div>

      {value.map((flow, index) => (
        <div class="admin-list-item" key={index}>
          <div class="admin-list-item-header">
            <strong>{flow.id || `Flow ${index + 1}`}</strong>
            <button class="admin-btn admin-btn--danger" onClick={() => remove(index)}>
              Remove
            </button>
          </div>
          <div class="admin-field-row">
            <TextField
              label="Id"
              value={flow.id}
              onChange={(v) => update(index, { id: v })}
              error={errors.get(`flows.${index}.id`)}
            />
            <TextField
              label="When to start it (the AI reads this)"
              value={flow.description}
              onChange={(v) => update(index, { description: v })}
            />
          </div>
          <div class="admin-field">
            <label>Message (shown word for word)</label>
            <textarea
              rows={3}
              value={flow.say}
              onInput={(e) => update(index, { say: (e.target as HTMLTextAreaElement).value })}
            />
            {errors.get(`flows.${index}.say`) && (
              <span class="admin-field-error">{errors.get(`flows.${index}.say`)}</span>
            )}
          </div>
          <div class="admin-field">
            <label>Buttons (shown in this order)</label>
            {flow.buttons.map((slug, at) => (
              <div class="admin-field-row" key={slug}>
                <span>
                  {at + 1}. {links.find((l) => l.slug === slug)?.label || slug}
                </span>
                <button class="admin-btn admin-btn--secondary" onClick={() => moveButton(index, at, -1)}>
                  ↑
                </button>
                <button class="admin-btn admin-btn--secondary" onClick={() => moveButton(index, at, 1)}>
                  ↓
                </button>
              </div>
            ))}
            {links.length === 0 && (
              <span class="admin-muted">Add some buttons above first.</span>
            )}
            {links.map((link) => (
              <CheckboxField
                key={link.slug}
                label={`${link.label || link.slug} (${link.type})`}
                checked={flow.buttons.includes(link.slug)}
                onChange={(on) => toggleButton(index, link.slug, on)}
              />
            ))}
            {errors.get(`flows.${index}.buttons`) && (
              <span class="admin-field-error">{errors.get(`flows.${index}.buttons`)}</span>
            )}
          </div>
        </div>
      ))}
      <button class="admin-btn admin-btn--secondary" onClick={add}>
        Add flow
      </button>
    </div>
  );
}

function UiSection({
  value,
  onChange,
  errors,
}: {
  value: UiValue;
  onChange: (v: UiValue) => void;
  errors: FieldErrors;
}) {
  return (
    <div class="admin-section">
      <h2>Interface</h2>
      <p class="admin-muted">
        What the bot is allowed to put on screen. <strong>All on by default</strong> — the bot
        builds its own buttons, quick replies and image cards from whatever your AI Prompt tells
        it to, with nothing configured here. These are switches for turning that off, not for
        turning it on.
      </p>
      <p class="admin-muted">
        The Buttons and Flows sections above are for the cases where you want to pin something
        down exactly — a link that must always be right, or a menu that must always say the same
        words. Everything else the bot can invent as it goes.
      </p>
      <div class="admin-field-row">
        <CheckboxField
          label="Buttons and quick replies"
          checked={value.buttons}
          onChange={(v) => onChange({ ...value, buttons: v })}
        />
        <CheckboxField
          label="Image cards"
          checked={value.cards}
          onChange={(v) => onChange({ ...value, cards: v })}
        />
        <CheckboxField
          label="Let the bot write its own opening message"
          checked={value.opening_turn}
          onChange={(v) => onChange({ ...value, opening_turn: v })}
        />
      </div>
      <p class="admin-muted">
        "Opening message" runs one real AI turn as the chat opens, so the bot can greet people
        with buttons instead of the fixed greeting above. It costs one request per visitor who
        opens the widget, including those who never type. Ignored when you've set a menu flow —
        that's already instant and free.
      </p>
      <ListField
        label="Allowed link hosts (empty = any; e.g. amazon.com or *.media-amazon.com)"
        values={value.allowed_hosts}
        onChange={(v) => onChange({ ...value, allowed_hosts: v })}
      />
      <p class="admin-muted">
        Restricts URLs the <em>bot</em> comes up with, never ones you entered in Buttons above.
        Leave it empty unless you have a reason — a bot that can only link where you've said is
        also a bot that can't link to something useful it found.
      </p>
      <NumberField
        label="Max cards per message"
        value={value.max_cards}
        onChange={(v) => onChange({ ...value, max_cards: v })}
        error={errors.get("ui.max_cards")}
      />
    </div>
  );
}

function ChannelsSection({
  value,
  onChange,
}: {
  value: ChannelsValue;
  onChange: (v: ChannelsValue) => void;
}) {
  return (
    <div class="admin-section">
      <h2>Channels</h2>
      <p class="admin-muted">
        Turning a channel off 404s that door for this bot on the very next deploy — the other
        channel is never affected.
      </p>
      <div class="admin-field-row">
        <CheckboxField
          label="Chat enabled"
          checked={value.chat.enabled}
          onChange={(v) => onChange({ ...value, chat: { enabled: v } })}
        />
        <CheckboxField
          label="Voice enabled"
          checked={value.voice.enabled}
          onChange={(v) => onChange({ ...value, voice: { enabled: v } })}
        />
      </div>
    </div>
  );
}
