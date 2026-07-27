"""Guards the deploy config itself (Phase 7 Step 2/3), the same idea as
test_migrations.py's RLS lint applied to infra/Dockerfile and .dockerignore.

This is how the original gap arose: `infra/Dockerfile` ran a bare
`pip install .` with no extras, so the deployed image could use neither
the Postgres checkpointer, MCP tools, nor LLM_PROVIDER=google -- entirely
invisible until someone actually ran the container. These tests can't
prove the image *works* (that needs a real `docker build`, done live in
Step 2/9), but they permanently close "someone edited the Dockerfile and
silently dropped an extra/flag".
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = (REPO_ROOT / "infra" / "Dockerfile").read_text(encoding="utf-8")
DOCKERIGNORE = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8")
LOCKFILE = REPO_ROOT / "infra" / "requirements.lock.txt"


def test_runtime_stage_installs_from_the_lockfile():
    assert "pip install -r requirements.lock.txt" in DOCKERFILE


def test_deps_stage_installs_the_production_extras():
    assert '".[postgres,mcp,google]"' in DOCKERFILE


def test_lockfile_exists_and_is_non_empty():
    assert LOCKFILE.is_file(), "infra/requirements.lock.txt must be committed (Step 1)"
    lines = [line for line in LOCKFILE.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert lines, "requirements.lock.txt is empty"
    assert all("==" in line for line in lines), "every pinned line must be an exact version"


def test_lockfile_does_not_pin_the_app_itself():
    """The runtime stage installs the app package separately via
    `pip install --no-deps .` -- a self-referential entry in the lockfile
    would point at a build path that doesn't exist in a fresh build."""
    text = LOCKFILE.read_text(encoding="utf-8").lower()
    assert "ai-receptionist" not in text and "ai_receptionist" not in text


def test_honours_the_platform_port():
    assert "${PORT:-8000}" in DOCKERFILE


def test_trusts_the_proxy_for_client_ip_and_scheme():
    assert "--proxy-headers" in DOCKERFILE


def test_gives_in_flight_streams_a_drain_deadline_on_redeploy():
    assert "--timeout-graceful-shutdown" in DOCKERFILE


def test_has_a_healthcheck():
    assert "HEALTHCHECK" in DOCKERFILE


def test_runs_as_a_non_root_user():
    assert "USER appuser" in DOCKERFILE
    assert "USER root" not in DOCKERFILE


def test_dockerignore_excludes_the_dev_venv_and_node_modules():
    assert ".venv/" in DOCKERIGNORE
    assert "node_modules/" in DOCKERIGNORE
    assert ".git/" in DOCKERIGNORE


def test_dockerignore_excludes_secrets():
    assert ".env" in DOCKERIGNORE
