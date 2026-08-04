import { useEffect, useState } from "preact/hooks";

import {
  ApiError,
  archiveTenant,
  getTenantConfig,
  purgeTenant,
  restoreTenant,
  saveTenantConfig,
  SessionInfo,
  TenantConfig,
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

export function ConfigView({ tenantId, session }: { tenantId: string; session: SessionInfo }) {
  const isOperator = session.kind === "operator";
  const [config, setConfig] = useState<TenantConfig | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<FieldErrors>(new Map());
  const [saving, setSaving] = useState(false);
  const [savedNotice, setSavedNotice] = useState(false);

  function load(): void {
    setConfig(null);
    setError(null);
    getTenantConfig(tenantId)
      .then(setConfig)
      .catch((err) => setError(err instanceof ApiError ? err.message : "failed to load config"));
  }

  useEffect(load, [tenantId]);

  async function save(): Promise<void> {
    if (!config) return;
    setSaving(true);
    setError(null);
    setFieldErrors(new Map());
    setSavedNotice(false);
    try {
      const saved = await saveTenantConfig(tenantId, config, config._version);
      setConfig(saved);
      setSavedNotice(true);
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

  function update<K extends keyof TenantConfig>(key: K, value: TenantConfig[K]): void {
    if (!config) return;
    setConfig({ ...config, [key]: value });
  }

  if (error && !config) {
    return <div class="admin-error-banner">{error}</div>;
  }
  if (!config) {
    return <p class="admin-muted">Loading…</p>;
  }

  const health = config._health;

  return (
    <div>
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
      {savedNotice && <div class="admin-tile" style={{ marginBottom: "1rem" }}>Saved.</div>}

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

      <div class="admin-section">
        <h2>Vapi wiring (read-only — change via `provision_vapi`, not here)</h2>
        <pre style={{ fontSize: "0.8rem" }}>{JSON.stringify(config.vapi, null, 2)}</pre>
      </div>

      <button class="admin-btn" onClick={save} disabled={saving}>
        {saving ? "Saving…" : "Save changes"}
      </button>

      {isOperator && (
        <DangerZone tenantId={tenantId} status={config.status as string} onChanged={load} />
      )}
    </div>
  );
}

function DangerZone({
  tenantId,
  status,
  onChanged,
}: {
  tenantId: string;
  status: string;
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
  return (
    <div class="admin-field">
      <label>{label} (comma-separated)</label>
      <input
        type="text"
        value={values.join(", ")}
        disabled={disabled}
        onInput={(e) =>
          onChange(
            (e.target as HTMLInputElement).value
              .split(",")
              .map((s) => s.trim())
              .filter(Boolean),
          )
        }
      />
    </div>
  );
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
