"""A standalone demo MCP server (Phase 6) — proves the whole path with zero
accounts and zero credentials.

Not imported by the app, not on any request path, and needs only the `mcp`
extra (`pip install -e ".[mcp]"`). It exposes two tools that are obviously
*not* tier-1 — the kind of long-tail lookup an MCP server is actually for —
returning canned, hotel-shaped data so the live check in `plans/phase6.md`
Step 10 has something concrete to ask about.

Run it:

    python -m scripts.demo_mcp_server --port 8765

Then point a tenant at it in `content/tenants/<id>.json`:

    "mcp_servers": [
        {"name": "demo", "transport": "http", "url": "http://127.0.0.1:8765/mcp"}
    ]

and set `MCP_ENABLED=true` (`MCP_SOURCE` can stay "json" for this).
"""

from __future__ import annotations

import argparse

from mcp.server.fastmcp import FastMCP

_LOYALTY_TIERS = {
    "5551234567": {"tier": "Gold", "points": 4820, "member_since": "2022"},
    "5559876543": {"tier": "Silver", "points": 1150, "member_since": "2024"},
}

_LOCAL_EVENTS = [
    {"name": "Riverside Night Market", "when": "Friday 6pm-11pm", "distance_km": 1.2},
    {"name": "Old Town Jazz Walk", "when": "Saturday 7pm-10pm", "distance_km": 2.5},
    {"name": "Sunday Farmers Market", "when": "Sunday 8am-1pm", "distance_km": 0.8},
]


def build_server() -> FastMCP:
    server = FastMCP(name="ai-receptionist-demo")

    @server.tool()
    def lookup_guest_loyalty(phone_number: str) -> dict:
        """Look up a guest's loyalty tier and points by phone number.

        Returns a canned record for two demo numbers, and a plain "no record
        found" for anything else — this is fixture data, not a real CRM.
        """
        digits = "".join(ch for ch in phone_number if ch.isdigit())
        record = _LOYALTY_TIERS.get(digits[-10:])
        if record is None:
            return {"found": False, "phone_number": phone_number}
        return {"found": True, "phone_number": phone_number, **record}

    @server.tool()
    def local_events_this_week() -> list[dict]:
        """List a few things happening near the hotel this week (demo data)."""
        return _LOCAL_EVENTS

    return server


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    server = build_server()
    server.settings.host = args.host
    server.settings.port = args.port
    print(f"demo MCP server listening on http://{args.host}:{args.port}/mcp")
    server.run(transport="streamable-http")


if __name__ == "__main__":
    main()
