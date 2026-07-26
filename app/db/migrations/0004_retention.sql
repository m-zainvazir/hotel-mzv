-- Phase 4 Step 7 — free-tier retention.
--
-- LangGraph's OSS Postgres checkpointer has no TTL (that's a LangGraph
-- Platform feature) and never UPDATEs a checkpoint row — it INSERTs a full
-- serialized snapshot per superstep. Our graph runs ~5 node executions per
-- turn (resolve_tenant -> emergency_check -> reason <-> tools), so a
-- ten-turn booking call writes ~50 rows. Real-world reports show ~56MB/18k
-- rows after a week of staging traffic. Our conversations live for minutes,
-- so a 48h window loses nothing real.
--
-- `pg_cron` runs INSIDE Postgres — no external scheduler, no second
-- service, consistent with "one roof". Verified live: `checkpoints.checkpoint`
-- is a jsonb column whose `ts` key is an ISO-8601 string
-- (langgraph.checkpoint.base.empty_checkpoint()), which is what's filtered
-- below rather than a guessed timestamp column.
--
-- Prerequisite: the `langgraph` schema and its tables only exist after the
-- app has connected at least once (AsyncPostgresSaver.setup() creates them —
-- see app/db/checkpointer.py). Apply this migration AFTER that first
-- connection, or the scheduled job will simply error harmlessly until then.

create extension if not exists pg_cron;

select cron.schedule(
    'prune-checkpoints',
    '0 * * * *',  -- hourly
    $$
    delete from langgraph.checkpoints
    where (checkpoint->>'ts')::timestamptz < now() - interval '48 hours';
    $$
);

select cron.schedule(
    'prune-checkpoint-writes',
    '5 * * * *',  -- offset from the checkpoints job by 5 min
    $$
    delete from langgraph.checkpoint_writes cw
    where not exists (
        select 1 from langgraph.checkpoints c
        where c.thread_id = cw.thread_id
          and c.checkpoint_ns = cw.checkpoint_ns
          and c.checkpoint_id = cw.checkpoint_id
    );
    $$
);

select cron.schedule(
    'prune-checkpoint-blobs',
    '10 * * * *',
    $$
    delete from langgraph.checkpoint_blobs cb
    where not exists (
        select 1 from langgraph.checkpoints c
        where c.thread_id = cb.thread_id
          and c.checkpoint_ns = cb.checkpoint_ns
    );
    $$
);

-- calls.transcript: recordings are URLs (cheap), but full transcripts
-- accumulate forever. Plan §16 already lists a PII retention policy as a
-- legal to-do — this satisfies both. 30 days is generous for support/dispute
-- lookback; tighten later if a shorter retention policy is adopted.
select cron.schedule(
    'prune-call-transcripts',
    '15 0 * * *',  -- daily
    $$
    update public.calls
    set transcript = null
    where transcript is not null
      and created_at < now() - interval '30 days';
    $$
);
