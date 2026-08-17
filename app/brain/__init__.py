"""The brain — one LangGraph graph serving every channel.

**These re-exports are lazy, and that is load-bearing (Phase 9.4).** Importing
them eagerly meant that touching *any* leaf of this package pulled in the whole
graph, and that closed two import cycles:

    app/tools/__init__.py -> registry -> action_tools -> app.flows
      -> app.flows.render -> app.brain.events -> [this file]
      -> app.brain.graph -> app.brain.nodes.reason -> app.tools.registry   (partial)

    ... -> app.brain.runner -> app.flows.render                            (partial)

`app/flows/render.py` wants one dataclass out of `app.brain.events`; it has no
business building a compiled graph to get it.

Both cycles were invisible for months because everything real — the app, every
script that touches `app.main`, and the entire test suite — imports the whole
package graph early enough that the loop is already resolved. What exposed them
was `scripts/authorize_calcom.py`, which legitimately imports only
`app.tenancy.secrets` and hit `ImportError: cannot import name
'native_tools_for' from partially initialized module 'app.tools.registry'` on
its first Vault read. `tests/test_import_cycles.py` guards this from a
subprocess, since module state is process-global and any earlier import in the
same interpreter hides the problem.

The public API is unchanged: `from app.brain import stream_turn` still works,
it just resolves on first attribute access (PEP 562) instead of at import time.
"""

from typing import Any

__all__ = [
    "BrainEvent",
    "ReceptionistState",
    "build_graph",
    "get_graph",
    "reset_graph",
    "run_turn",
    "stream_turn",
]

_EXPORTS = {
    "BrainEvent": "app.brain.events",
    "ReceptionistState": "app.brain.state",
    "build_graph": "app.brain.graph",
    "get_graph": "app.brain.graph",
    "reset_graph": "app.brain.graph",
    "run_turn": "app.brain.runner",
    "stream_turn": "app.brain.runner",
}


def __getattr__(name: str) -> Any:
    module_path = _EXPORTS.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    from importlib import import_module

    return getattr(import_module(module_path), name)


def __dir__() -> list[str]:
    return sorted(__all__)
