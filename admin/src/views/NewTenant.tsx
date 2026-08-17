import { useEffect, useState } from "preact/hooks";

import {
  ApiError,
  CreateTenantMode,
  createTenant,
  listTenantIds,
  TenantSummary,
} from "../api";
import { navigate, tenantUrl } from "../router";

const TEMPLATES = ["hotel", "clinic", "trades", "salon", "restaurant"];

//: Matches `TenantConfig.timezone`'s own default (app/tenancy/models.py), so
//: creating a bot and never touching this field lands on the same value the
//: server would have picked anyway.
const DEFAULT_TIMEZONE = "Asia/Karachi";

function timezoneChoices(): string[] {
  try {
    const supported = (
      Intl as unknown as { supportedValuesOf?: (k: string) => string[] }
    ).supportedValuesOf?.("timeZone");
    if (supported?.length) {
      // Pinned to the top: it's the default, and scrolling an alphabetical
      // 400-entry list to confirm it is busywork on every single new bot.
      return [DEFAULT_TIMEZONE, ...supported.filter((z) => z !== DEFAULT_TIMEZONE)];
    }
  } catch {
    /* fall through */
  }
  return [DEFAULT_TIMEZONE, "Asia/Dubai", "Europe/London", "America/New_York", "UTC"];
}

const timezoneOptions = timezoneChoices();

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

export function NewTenantView() {
  const [mode, setMode] = useState<CreateTenantMode>("blank");
  const [template, setTemplate] = useState(TEMPLATES[0]);
  const [sourceTenantId, setSourceTenantId] = useState("");
  const [tenants, setTenants] = useState<TenantSummary[]>([]);

  const [tenantId, setTenantId] = useState("");
  const [name, setName] = useState("");
  const [trade, setTrade] = useState("");
  const [greeting, setGreeting] = useState("");
  const [escalationPhone, setEscalationPhone] = useState("");
  // Phase 9.4. Kept as a string, not a number: an empty box has to mean
  // "no calendar", and a numeric input coerces that to 0 — which would read
  // as event type 0 and create a bot pointed at nothing.
  const [calcomEventTypeId, setCalcomEventTypeId] = useState("");
  const [timezone, setTimezone] = useState(DEFAULT_TIMEZONE);

  const [error, setError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<FieldErrors>(new Map());
  const [creating, setCreating] = useState(false);

  useEffect(() => {
    listTenantIds()
      .then((body) => {
        setTenants(body.tenants);
        if (body.tenants.length > 0) setSourceTenantId(body.tenants[0].tenant_id);
      })
      .catch(() => setTenants([]));
  }, []);

  async function submit(event: Event): Promise<void> {
    event.preventDefault();
    setError(null);
    setFieldErrors(new Map());
    setCreating(true);
    try {
      const created = await createTenant({
        mode,
        template: mode === "template" ? template : undefined,
        source_tenant_id: mode === "clone" ? sourceTenantId : undefined,
        tenant_id: tenantId.trim(),
        name: name.trim(),
        trade: trade.trim(),
        greeting: greeting.trim(),
        escalation_phone: escalationPhone.trim(),
        calcom_event_type_id: calcomEventTypeId.trim() ? Number(calcomEventTypeId.trim()) : null,
        timezone,
      });
      // `created` is a TenantDetail, so the id lives one level down. Reading
      // `created.tenant_id` here navigated to the literal string "undefined"
      // — the bot was created correctly and then the panel couldn't find it.
      navigate(tenantUrl(created.config.tenant_id, "config"));
    } catch (err) {
      if (err instanceof ApiError && err.status === 422) {
        setFieldErrors(parseFieldErrors(err.detail));
        setError("Fix the highlighted fields and try again.");
      } else if (err instanceof ApiError && err.status === 409) {
        setError(err.message);
      } else {
        setError(err instanceof Error ? err.message : "could not create the bot");
      }
    } finally {
      setCreating(false);
    }
  }

  return (
    <div>
      <h1>New bot</h1>
      <p class="admin-muted">
        Everything else defaults — fine-tune hours, services, booking and voice from the new
        bot's own Config tab afterward.
      </p>
      {error && <div class="admin-error-banner">{error}</div>}

      <form onSubmit={submit}>
        <div class="admin-section">
          <h2>Starting point</h2>
          <div class="admin-field-row">
            <label class="admin-radio">
              <input
                type="radio"
                name="mode"
                checked={mode === "blank"}
                onChange={() => setMode("blank")}
              />
              Blank
            </label>
            <label class="admin-radio">
              <input
                type="radio"
                name="mode"
                checked={mode === "template"}
                onChange={() => setMode("template")}
              />
              Template
            </label>
            <label class="admin-radio">
              <input
                type="radio"
                name="mode"
                checked={mode === "clone"}
                onChange={() => setMode("clone")}
                disabled={tenants.length === 0}
              />
              Clone an existing bot
            </label>
          </div>

          {mode === "template" && (
            <div class="admin-field">
              <label>Template</label>
              <select value={template} onChange={(e) => setTemplate((e.target as HTMLSelectElement).value)}>
                {TEMPLATES.map((t) => (
                  <option key={t} value={t}>
                    {t}
                  </option>
                ))}
              </select>
            </div>
          )}

          {mode === "clone" && (
            <div class="admin-field">
              <label>Clone from</label>
              <select
                value={sourceTenantId}
                onChange={(e) => setSourceTenantId((e.target as HTMLSelectElement).value)}
              >
                {tenants.map((t) => (
                  <option key={t.tenant_id} value={t.tenant_id}>
                    {t.tenant_id}
                  </option>
                ))}
              </select>
              <span class="admin-muted" style={{ fontSize: "0.8rem" }}>
                Phone numbers, widget key, Vapi assistant, voice and Cal.com event type are NOT
                copied — the clone starts unconfigured for those.
              </span>
            </div>
          )}
        </div>

        <div class="admin-section">
          <h2>Basics</h2>
          <div class="admin-field">
            <label>Tenant id (lowercase, digits, hyphens only — fixed after creation)</label>
            <input
              type="text"
              value={tenantId}
              onInput={(e) => setTenantId((e.target as HTMLInputElement).value)}
              placeholder="a-new-hotel"
            />
            {fieldErrors.get("tenant_id") && (
              <span class="admin-field-error">{fieldErrors.get("tenant_id")}</span>
            )}
          </div>
          <div class="admin-field-row">
            <div class="admin-field">
              <label>Name</label>
              <input type="text" value={name} onInput={(e) => setName((e.target as HTMLInputElement).value)} />
            </div>
            <div class="admin-field">
              <label>Trade</label>
              <input type="text" value={trade} onInput={(e) => setTrade((e.target as HTMLInputElement).value)} />
            </div>
          </div>
          <div class="admin-field">
            <label>Greeting</label>
            <textarea value={greeting} onInput={(e) => setGreeting((e.target as HTMLTextAreaElement).value)} />
          </div>
          <div class="admin-field">
            <label>Emergency escalation phone</label>
            <input
              type="text"
              value={escalationPhone}
              onInput={(e) => setEscalationPhone((e.target as HTMLInputElement).value)}
              placeholder="+15551234567"
            />
            {fieldErrors.get("emergency.escalation_phone") && (
              <span class="admin-field-error">{fieldErrors.get("emergency.escalation_phone")}</span>
            )}
          </div>
        </div>

        <div class="admin-section">
          <h2>Calendar</h2>
          <p class="admin-muted">
            Connect a Cal.com calendar and it decides everything about availability — which days,
            which hours, what's already booked. Leave this blank and the bot uses a simulated
            calendar with hours you set by hand, which is fine for trying it out.
          </p>
          <div class="admin-field-row">
            <div class="admin-field">
              <label>Cal.com event type ID (optional)</label>
              <input
                type="text"
                inputMode="numeric"
                value={calcomEventTypeId}
                onInput={(e) => setCalcomEventTypeId((e.target as HTMLInputElement).value)}
                placeholder="6446177"
              />
              <span class="admin-muted" style={{ fontSize: "0.8rem" }}>
                In Cal.com, open the event type and copy the number from its URL.
              </span>
              {fieldErrors.get("calcom_event_type_id") && (
                <span class="admin-field-error">{fieldErrors.get("calcom_event_type_id")}</span>
              )}
            </div>
            <div class="admin-field">
              <label>Timezone</label>
              <select
                value={timezone}
                onChange={(e) => setTimezone((e.target as HTMLSelectElement).value)}
              >
                {timezoneOptions.map((zone) => (
                  <option value={zone} key={zone}>
                    {zone}
                  </option>
                ))}
              </select>
              {fieldErrors.get("timezone") && (
                <span class="admin-field-error">{fieldErrors.get("timezone")}</span>
              )}
            </div>
          </div>
          {calcomEventTypeId.trim() !== "" && (
            <div class="admin-tile">
              One more step after creating this bot: authorize it against the Cal.com account
              that owns the calendar, by running{" "}
              <code>
                python -m scripts.authorize_calcom --tenant {tenantId.trim() || "<tenant-id>"}
              </code>
              . Until then it won't be able to check availability or book anything.
            </div>
          )}
        </div>

        <button class="admin-btn" type="submit" disabled={creating}>
          {creating ? "Creating…" : "Create bot"}
        </button>
      </form>
    </div>
  );
}
