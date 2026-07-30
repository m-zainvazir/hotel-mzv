import { useEffect, useState } from "preact/hooks";

import { ApiError, Escalation, JobRow, listEscalations, listJobs } from "../api";

export function JobsEscalationsView({ tenantId }: { tenantId: string }) {
  const [jobs, setJobs] = useState<JobRow[] | null>(null);
  const [escalations, setEscalations] = useState<Escalation[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([listJobs(tenantId), listEscalations(tenantId)])
      .then(([jobsBody, escalationsBody]) => {
        setJobs(jobsBody.jobs);
        setEscalations(escalationsBody.escalations);
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : "failed to load"));
  }, [tenantId]);

  return (
    <div>
      {error && <div class="admin-error-banner">{error}</div>}

      <div class="admin-section">
        <h2>Bookings</h2>
        {jobs === null && !error && <p class="admin-muted">Loading…</p>}
        {jobs && jobs.length === 0 && <p class="admin-muted">No bookings recorded yet.</p>}
        {jobs && jobs.length > 0 && (
          <table class="admin-table">
            <thead>
              <tr>
                <th>Service</th>
                <th>Customer</th>
                <th>When</th>
                <th>Status</th>
                <th>Channel</th>
              </tr>
            </thead>
            <tbody>
              {jobs.map((job) => (
                <tr key={job.id}>
                  <td>{job.service_name}</td>
                  <td>{job.customer_name}</td>
                  <td>{job.scheduled_start}</td>
                  <td>{job.status}</td>
                  <td>{job.channel}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div class="admin-section">
        <h2>Escalations</h2>
        {escalations === null && !error && <p class="admin-muted">Loading…</p>}
        {escalations && escalations.length === 0 && (
          <p class="admin-muted">No escalations recorded yet.</p>
        )}
        {escalations && escalations.length > 0 && (
          <table class="admin-table">
            <thead>
              <tr>
                <th>When</th>
                <th>Reason</th>
                <th>Transferred to</th>
                <th>Channel</th>
              </tr>
            </thead>
            <tbody>
              {escalations.map((escalation) => (
                <tr key={escalation.id}>
                  <td>{escalation.created_at}</td>
                  <td>{escalation.reason}</td>
                  <td>{escalation.transferred_to}</td>
                  <td>{escalation.channel}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
