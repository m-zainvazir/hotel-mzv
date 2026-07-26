"""Preflight the configured model before you trust it with a call.

    python -m scripts.check_model            # check what .env is set to
    python -m scripts.check_model --all      # survey every model your key can see

The booking flow is *entirely* tool calls, so a model that can't call tools is
useless here — but it fails softly: it will chat away happily and simply never
book anything. That looks like it's working right up until you check the diary.
This catches it in seconds instead.
"""

from __future__ import annotations

import argparse
import sys

import httpx
from rich.console import Console
from rich.table import Table

from app.config import get_settings
from app.logging_config import force_utf8_console

console = Console()

GROQ_BASE = "https://api.groq.com/openai/v1"
GOOGLE_BASE = "https://generativelanguage.googleapis.com/v1beta"

#: Models that aren't conversational assistants, so not worth surveying.
_SKIP = ("whisper", "tts", "guard", "embed", "orpheus", "aqa", "imagen", "veo")

PROBE_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "check_availability",
            "description": "Find appointment slots.",
            "parameters": {
                "type": "object",
                "properties": {"service": {"type": "string"}},
                "required": ["service"],
            },
        },
    }
]


def _error_of(response: httpx.Response) -> str:
    try:
        return str(response.json().get("error", {}).get("message", ""))[:70]
    except Exception:
        return f"HTTP {response.status_code}"


# --- OpenAI-shaped providers (Groq, OpenAI, and its compatibles) ------------


def _openai_probe(key: str, model: str, base: str) -> tuple[bool, bool, str]:
    body = {"model": model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5}
    headers = {"Authorization": f"Bearer {key}", "content-type": "application/json"}
    try:
        plain = httpx.post(f"{base}/chat/completions", headers=headers, json=body, timeout=40)
    except Exception as exc:  # network, DNS, timeout
        return False, False, str(exc)[:60]

    if plain.status_code != 200:
        return False, False, _error_of(plain)

    with_tools = httpx.post(
        f"{base}/chat/completions", headers=headers, json={**body, "tools": PROBE_TOOL}, timeout=40
    )
    if with_tools.status_code == 200:
        return True, True, ""
    return True, False, _error_of(with_tools)


def _openai_list(key: str, base: str) -> list[str]:
    headers = {"Authorization": f"Bearer {key}"}
    listing = httpx.get(f"{base}/models", headers=headers, timeout=30)
    listing.raise_for_status()
    return sorted(m["id"] for m in listing.json().get("data", []))


# --- Google Gemini (native REST — not OpenAI-shaped) ------------------------


def _google_probe(key: str, model: str, base: str) -> tuple[bool, bool, str]:
    name = model if model.startswith("models/") else f"models/{model}"
    url = f"{base}/{name}:generateContent?key={key}"
    body: dict = {"contents": [{"role": "user", "parts": [{"text": "hi"}]}]}
    try:
        plain = httpx.post(url, json=body, timeout=40)
    except Exception as exc:
        return False, False, str(exc)[:60]

    if plain.status_code != 200:
        return False, False, _error_of(plain)

    tool = {"function_declarations": [PROBE_TOOL[0]["function"]]}
    with_tools = httpx.post(url, json={**body, "tools": [tool]}, timeout=40)
    if with_tools.status_code == 200:
        return True, True, ""
    return True, False, _error_of(with_tools)


def _google_list(key: str, base: str) -> list[str]:
    listing = httpx.get(f"{base}/models?key={key}&pageSize=200", timeout=30)
    listing.raise_for_status()
    out = []
    for m in listing.json().get("models", []):
        methods = m.get("supportedGenerationMethods", [])
        if "generateContent" in methods:
            out.append(m["name"].removeprefix("models/"))
    return sorted(out)


def _dispatch(settings):
    """(probe_fn, list_fn, key, base, model) for the configured provider."""
    if settings.llm_provider == "google":
        return (
            _google_probe,
            _google_list,
            settings.google_api_key,
            GOOGLE_BASE,
            settings.google_model,
        )
    if settings.llm_provider == "groq":
        return _openai_probe, _openai_list, settings.groq_api_key, GROQ_BASE, settings.groq_model
    base = settings.openai_base_url or "https://api.openai.com/v1"
    return _openai_probe, _openai_list, settings.openai_api_key, base, settings.openai_model


def survey(probe_fn, list_fn, key: str, base: str) -> int:
    try:
        all_models = list_fn(key, base)
    except Exception as exc:
        console.print(f"[bold red]Cannot list models:[/bold red] {str(exc)[:80]}")
        return 1

    models = [m for m in all_models if not any(skip in m for skip in _SKIP)]

    table = Table(title="Tool-calling support (the booking flow needs it)")
    table.add_column("model")
    table.add_column("reachable")
    table.add_column("tool calling")
    table.add_column("note", overflow="fold")

    usable = 0
    for model in models:
        reachable, tools, note = probe_fn(key, model, base)
        if reachable and tools:
            usable += 1
        table.add_row(
            model,
            "[green]yes[/green]" if reachable else "[red]no[/red]",
            "[green]yes[/green]" if tools else "[red]NO[/red]",
            note,
        )

    console.print(table)
    console.print(f"\n[bold]{usable}[/bold] of {len(models)} models can run this project.")
    return 0


def main(argv: list[str] | None = None) -> int:
    force_utf8_console()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--all", action="store_true", help="survey every available model")
    args = parser.parse_args(argv)

    settings = get_settings()
    probe_fn, list_fn, key, base, model = _dispatch(settings)

    if not key:
        console.print(
            f"[bold red]No API key configured for provider {settings.llm_provider!r}.[/bold red]"
        )
        return 1

    if args.all:
        return survey(probe_fn, list_fn, key, base)

    console.print(f"provider [cyan]{settings.llm_provider}[/cyan]  model [cyan]{model}[/cyan]")
    reachable, tools, note = probe_fn(key, model, base)

    if not reachable:
        console.print(f"[bold red]✗ unreachable[/bold red] — {note}")
        console.print("[dim]A 401 here means the key; anything else usually means the model.[/dim]")
        return 1
    if not tools:
        console.print(f"[bold red]✗ {model} cannot call tools[/bold red] — {note}")
        console.print(
            "\nThis model would chat normally and [bold]never book anything[/bold].\n"
            "Pick a tool-capable model — run [cyan]--all[/cyan] to see which of yours qualify."
        )
        return 1

    console.print(f"[bold green]✓ {model} is reachable and supports tool calling.[/bold green]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
