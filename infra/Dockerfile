# The one service (plan §3). Deploy this to Railway / Fly / Render.
#
# Two stages:
#   - `deps`    installs the full dependency tree. Also the target used to
#     regenerate infra/requirements.lock.txt (see infra/README.md) — building
#     it inside this image, rather than freezing the Windows dev venv, is
#     what keeps win32-only pins (pywin32, the uuid-utils<0.16 cap) out of
#     the lockfile.
#   - `runtime` installs from the committed lockfile instead of re-resolving
#     from pyproject.toml, so every build of the same commit gets the same
#     versions (CLAUDE.md's uuid_utils and [google] gotchas are both cases of
#     this project's dependency graph resolving differently across builds).
#
# Build from the repo root: `docker build -f infra/Dockerfile .`

FROM python:3.12-slim AS deps

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

COPY pyproject.toml README.md ./
COPY app ./app
COPY scripts ./scripts

# `postgres` — durable checkpointer (Phase 4). `mcp` — long-tail tools
# (Phase 6). `google` — matches this project's current LLM_PROVIDER; if that
# ever changes to `openai`, add the `openai` extra here too. Both `google`
# and `openai` are safe to install unconditionally if you'd rather not track
# this, at the cost of a slightly larger image.
RUN pip install --upgrade pip && pip install ".[postgres,mcp,google]"


FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

# Dependencies first, from the lockfile — this layer only invalidates when
# the lockfile changes, not on every app code edit.
COPY pyproject.toml README.md infra/requirements.lock.txt ./
RUN pip install --upgrade pip && pip install -r requirements.lock.txt

COPY app ./app
COPY scripts ./scripts
# Editable content — prompt, tenant configs, filler phrases (see content/README.md).
# Read from disk at runtime, so it must ship in the image.
COPY content ./content
# The widget bundle is built on the host (npm --prefix widget run build) and
# committed to the repo — see widget/README.md and .gitignore. No Node in
# this image; only the already-built output + the static demo page ship.
COPY widget/dist ./widget/dist
COPY widget/demo.html ./widget/demo.html
# Same story for the admin dashboard (Phase 8) — built on the host
# (npm --prefix admin run build), committed, no Node in this image. Unlike
# widget/dist this COPY failing (bundle not built) fails the image build
# outright rather than degrading to a 404 — app/main.py guards the
# StaticFiles mount itself for the "built the image without building the
# bundle first" case in dev, but a committed repo should never be in that
# state at build time.
COPY admin/dist ./admin/dist

# --no-deps: every real dependency already came from the lockfile above.
# This installs only the ai-receptionist package itself (console scripts,
# egg metadata) without letting pip re-resolve anything.
RUN pip install --no-deps .

RUN useradd --create-home --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /srv
USER appuser

EXPOSE 8000

# python, not curl — curl isn't in python:3.12-slim and adding it is a whole
# extra apt-get layer for one healthcheck. Reads $PORT itself so it tracks
# whatever the CMD below actually bound.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/health', timeout=3)"]

# Single worker, single replica — non-negotiable, not a placeholder default.
# app/brain/metrics.py's TurnCounter, app/channels/widget_auth.py's
# in-process fallback secret, and app/channels/ratelimit.py's in-process
# limiter are all per-process state with no cross-process sharing. A second
# worker or a second replica makes all three silently wrong at once — see
# plans/phase7.md's "Decisions locked" table.
#
# `sh -c ... exec uvicorn ...`: `exec` replaces the shell so SIGTERM reaches
# uvicorn directly (a redeploy or scale-down sends SIGTERM; without `exec`
# it would hit the shell instead and uvicorn wouldn't drain). `--port
# ${PORT:-8000}` honours Railway's injected $PORT, which app/config.py's own
# host/port settings are never read for. `--proxy-headers
# --forwarded-allow-ips='*'` trusts Railway's edge proxy for the real client
# IP and scheme — required for app/channels/ratelimit.py's per-IP keys to see
# anything other than the proxy's own address, and for request.url.scheme to
# read https instead of http behind TLS termination.
# `--timeout-graceful-shutdown 25` gives in-flight SSE streams (a live voice
# turn or chat response) a drain deadline on redeploy instead of a hard cut.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*' --timeout-graceful-shutdown 25 --timeout-keep-alive 75"]
