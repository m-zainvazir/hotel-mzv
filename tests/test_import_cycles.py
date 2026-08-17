"""Import-order regressions (Phase 9.4).

Every other test in this suite imports `app.main` (or something that reaches
it) long before it touches a submodule, so the whole package graph is already
resolved and a cycle is invisible. A `scripts/` entry point doesn't work that
way: it imports the two or three modules it actually needs.

`scripts/authorize_calcom.py` crashed on exactly that difference — it reaches
`app.tenancy.secrets._shared_client()`, whose lazy `app.tools.http_client`
import walks `app.tools.__init__` -> registry -> action_tools -> app.flows ->
app.brain -> app.brain.nodes.reason, which used to import back into the
half-initialized `app.tools.registry`.

Each test runs in a **subprocess** because module imports are process-global:
once any earlier test in this session has imported `app.main`, the cycle is
permanently hidden inside this interpreter.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

#: Entry points a script legitimately starts from, each of which used to be
#: enough to trip the cycle. `app.tenancy.secrets` is the one that actually
#: bit; the rest are the same shape and cost nothing to pin.
_COLD_IMPORTS = [
    # The real failure: authorize_calcom's path to a Vault read.
    "import app.tenancy.secrets; app.tenancy.secrets._shared_client",
    "import app.tools.booking.schedule",
    "import app.brain.nodes.reason",
    "import app.tools.registry",
    "import app.flows",
    "import app.mcp.oauth",
]


@pytest.mark.parametrize("statement", _COLD_IMPORTS)
def test_importable_without_importing_app_main_first(statement: str):
    """A cold `python -c "<statement>"` must not raise ImportError."""
    result = subprocess.run(
        [sys.executable, "-c", statement],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"cold import failed — likely a new import cycle:\n{result.stderr[-1500:]}"
    )


def test_the_vault_read_path_builds_its_client_cold():
    """The precise call that crashed: resolving a per-tenant secret's HTTP
    client with nothing else imported. Doesn't perform a request — building
    the client is what triggered the cycle."""
    script = (
        "import os\n"
        "os.environ.setdefault('SUPABASE_URL', 'https://example.supabase.co')\n"
        "import app.tenancy.secrets as secrets\n"
        "assert secrets._shared_client() is not None\n"
        "print('ok')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr[-1500:]
    assert "ok" in result.stdout
