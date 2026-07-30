import { useEffect, useState } from "preact/hooks";

import { ApiError, DailyMetric, getTenantMetrics, TenantMetrics } from "../api";
import { BarChart } from "../charts/BarChart";

type Window = "7" | "30" | "90";

export function MetricsView({ tenantId }: { tenantId: string }) {
  const [windowDays, setWindowDays] = useState<Window>("30");
  const [totals, setTotals] = useState<TenantMetrics | null>(null);
  const [daily, setDaily] = useState<DailyMetric[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const until = new Date();
    const since = new Date(until);
    since.setDate(since.getDate() - Number(windowDays));
    const toDateOnly = (d: Date) => d.toISOString().slice(0, 10);

    getTenantMetrics(tenantId, toDateOnly(since), toDateOnly(until))
      .then((body) => {
        setTotals(body.totals);
        setDaily(body.daily);
        setError(null);
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : "failed to load metrics"));
  }, [tenantId, windowDays]);

  return (
    <div>
      <div class="admin-field-row" style={{ alignItems: "center", marginBottom: "0.5rem" }}>
        <label class="admin-muted" for="window-select">
          Window
        </label>
        <select
          id="window-select"
          value={windowDays}
          onChange={(event) => setWindowDays((event.target as HTMLSelectElement).value as Window)}
        >
          <option value="7">Last 7 days</option>
          <option value="30">Last 30 days</option>
          <option value="90">Last 90 days</option>
        </select>
      </div>

      {error && <div class="admin-error-banner">{error}</div>}

      {totals && (
        <div class="admin-tiles">
          <Tile label="Voice calls" value={totals.calls} />
          <Tile label="Chat sessions" value={totals.chat_sessions} />
          <Tile label="Bookings" value={totals.jobs} />
          <Tile label="Escalations" value={totals.escalations} />
          <Tile label="Call minutes" value={Math.round(totals.call_seconds / 60)} />
          <Tile label="Vapi telephony cost" value={`$${totals.cost_usd.toFixed(2)}`} />
        </div>
      )}

      {daily.length > 0 && (
        <>
          <div class="admin-section">
            <h2>Conversations / day</h2>
            <BarChart
              values={daily.map((d) => d.calls + d.chat_sessions)}
              labels={daily.map((d) => d.day)}
            />
          </div>
          <div class="admin-section">
            <h2>Bookings / day</h2>
            <BarChart
              values={daily.map((d) => d.jobs)}
              labels={daily.map((d) => d.day)}
              color="#2563eb"
            />
          </div>
        </>
      )}

      <p class="admin-muted">
        No per-tenant LLM cost or per-turn latency is tracked — "Vapi telephony cost" is exactly
        that, not a total cost figure.
      </p>
    </div>
  );
}

function Tile({ label, value }: { label: string; value: number | string }) {
  return (
    <div class="admin-tile">
      <div class="admin-tile-label">{label}</div>
      <div class="admin-tile-value">{value}</div>
    </div>
  );
}
