# The one service (plan §3). Deploy this to Railway / Fly / Render.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

COPY pyproject.toml README.md ./
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

RUN pip install --upgrade pip && pip install .

EXPOSE 8000

# Co-locate the deployment with Vapi + Groq regions to protect the latency
# budget (plan §13).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
