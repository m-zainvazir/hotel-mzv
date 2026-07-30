import { useEffect, useState } from "preact/hooks";

import { ApiError, CallSummary, getCall, listCalls } from "../api";

export function CallsView({ tenantId }: { tenantId: string }) {
  const [calls, setCalls] = useState<CallSummary[] | null>(null);
  const [selected, setSelected] = useState<Record<string, unknown> | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setSelected(null);
    listCalls(tenantId)
      .then((body) => setCalls(body.calls))
      .catch((err) => setError(err instanceof ApiError ? err.message : "failed to load calls"));
  }, [tenantId]);

  async function openCall(callId: string) {
    try {
      const call = await getCall(tenantId, callId);
      setSelected(call);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "failed to load call");
    }
  }

  return (
    <div>
      {error && <div class="admin-error-banner">{error}</div>}
      {calls === null && !error && <p class="admin-muted">Loading…</p>}
      {calls && calls.length === 0 && <p class="admin-muted">No calls recorded yet.</p>}
      {calls && calls.length > 0 && (
        <table class="admin-table">
          <thead>
            <tr>
              <th>Started</th>
              <th>From</th>
              <th>Duration</th>
              <th>Ended reason</th>
              <th>Cost</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {calls.map((call) => (
              <tr key={call.id}>
                <td>{call.started_at ?? call.created_at}</td>
                <td>{call.from_number ?? "—"}</td>
                <td>{call.duration_seconds ? `${Math.round(call.duration_seconds)}s` : "—"}</td>
                <td>
                  {call.ended_reason?.startsWith("error") ? (
                    <span class="admin-badge admin-badge--warn">{call.ended_reason}</span>
                  ) : (
                    (call.ended_reason ?? "—")
                  )}
                </td>
                <td>{call.cost_usd != null ? `$${call.cost_usd.toFixed(3)}` : "—"}</td>
                <td>
                  <button class="admin-btn admin-btn--secondary" onClick={() => openCall(call.id)}>
                    View transcript
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {selected && (
        <div class="admin-section" style={{ marginTop: "1rem" }}>
          <h2>Call transcript — {String(selected.id)}</h2>
          <p class="admin-muted">
            Reached only here, on this explicit action — never in the calls list above.
          </p>
          <pre style={{ whiteSpace: "pre-wrap", fontSize: "0.85rem" }}>
            {String(selected.transcript ?? "(no transcript recorded)")}
          </pre>
          {Boolean(selected.recording_url) && (
            <p>
              <a href={String(selected.recording_url)} target="_blank" rel="noreferrer">
                Recording
              </a>
            </p>
          )}
        </div>
      )}
    </div>
  );
}
