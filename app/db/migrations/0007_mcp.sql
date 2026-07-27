-- Phase 6 — the MCP registry gets three columns the original table (0001_schema.sql)
-- didn't need until there was a loader to read it.
--
-- IMPORTANT: apply this together with 0006_chat.sql, which is still unapplied to the
-- live project as of Phase 5 (see CLAUDE.md "Current state") — do both in one sitting.
--
-- This migration creates no table, so it needs no RLS/FORCE/policy/grant statements of
-- its own: public.mcp_servers already has all four from 0002_rls.sql, and
-- tests/test_migrations.py's lint only checks `create table` statements.
--
-- Column notes:
--   enabled          — a server row can be registered but paused without deleting it
--                       (scripts/register_mcp_server.py --disable).
--   headers          — may contain literal '${secret}', substituted from the vault
--                       value at auth_secret_ref by app/mcp/connections.py. Never store
--                       a resolved secret here — this table is plain PostgREST-readable
--                       config, not Vault.
--   tool_allowlist   — empty means "every tool this server offers"; the per-tenant
--                       scalpel against the blunt MCP_MAX_TOOLS cap.

alter table public.mcp_servers
    add column if not exists enabled boolean not null default true,
    add column if not exists headers jsonb not null default '{}'::jsonb,
    add column if not exists tool_allowlist text[] not null default '{}';
