import { useEffect, useState } from "preact/hooks";

import { ApiError, getOverview, OverviewRow } from "../api";
import { tenantUrl } from "../router";

export function Overview() {
  const [rows, setRows] = useState<OverviewRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getOverview()
      .then((body) => setRows(body.tenants))
      .catch((err) => setError(err instanceof ApiError ? err.message : "failed to load overview"));
  }, []);

  return (
    <div>
      <h1>Overview</h1>
      <p class="admin-muted">
        Last 30 days, per tenant. A tenant's own row degrades independently if its metrics fail
        to load — one Supabase hiccup never blanks the whole page.
      </p>
      {error && <div class="admin-error-banner">{error}</div>}
      {rows === null && !error && <p class="admin-muted">Loading…</p>}
      {rows && (
        <table class="admin-table">
          <thead>
            <tr>
              <th>Tenant</th>
              <th>Trade</th>
              <th>Status</th>
              <th>Conversations</th>
              <th>Bookings</th>
              <th>Escalations</th>
              <th>Call minutes</th>
              <th>Vapi cost</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.tenant_id}>
                <td>
                  <a href={tenantUrl(row.tenant_id)}>{row.name ?? row.tenant_id}</a>
                </td>
                <td>{row.trade ?? "—"}</td>
                <td>{row.status ?? "—"}</td>
                {row.error ? (
                  <td colSpan={5} class="admin-field-error">
                    {row.error}
                  </td>
                ) : (
                  <>
                    <td>{(row.metrics?.calls ?? 0) + (row.metrics?.chat_sessions ?? 0)}</td>
                    <td>{row.metrics?.jobs ?? 0}</td>
                    <td>{row.metrics?.escalations ?? 0}</td>
                    <td>{Math.round((row.metrics?.call_seconds ?? 0) / 60)}</td>
                    <td>${(row.metrics?.cost_usd ?? 0).toFixed(2)}</td>
                  </>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
