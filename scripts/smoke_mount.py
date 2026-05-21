"""Smoke test: burr-mcp's mount() accepts an ansiburr-built Application
and the resulting MCP server can drive at least one ansible-backed action
to completion in-process via the fastmcp Client.

Run with: uv run python scripts/smoke_mount.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "examples"))

from burrmcp import ServingMode, mount  # noqa: E402
from fastmcp import Client  # noqa: E402
from localhost_disk_check import build_application  # noqa: E402


async def main() -> int:
    server = mount(
        build_application,
        mode=ServingMode.STEP,
        name="ansiburr-smoke",
    )

    async with Client(server) as client:
        tools = await client.list_tools()
        tool_names = sorted(t.name for t in tools)
        print(f"MCP tools exposed: {tool_names}")

        resources = await client.list_resources()
        print(f"MCP resources exposed: {sorted(str(r.uri) for r in resources)}")

        print("\nDriving the FSM via the step tool...")
        step_count = 0
        seen_actions: list[str] = []
        while step_count < 10:
            choices = json.loads((await client.read_resource("burr://next"))[0].text)
            if not choices:
                print("  no further reachable actions; halt.")
                break
            next_action = choices[0]
            seen_actions.append(next_action)
            print(f"  step {step_count}: reachable={choices}; calling step({next_action})")
            result = await client.call_tool("step", {"action": next_action})
            payload = json.loads(result.content[0].text)
            if payload.get("terminal"):
                print(f"  terminal reached at action={next_action}")
                break
            step_count += 1

        session = json.loads((await client.read_resource("burr://session"))[0].text)
        state = json.loads((await client.read_resource("burr://state"))[0].text)
        print(f"\nFinal app_id: {session.get('app_id')}")
        print(f"Final state keys: {sorted(state.keys())}")
        print(f"  status: {state.get('status')!r}")
        print(f"  usage_pct: {state.get('usage_pct')!r}")
        print(f"  uptime present: {bool(state.get('uptime_stdout'))}")
        print(f"Visited actions: {seen_actions}")

    if state.get("status", "").startswith(("ok", "WARN")):
        print("\nSMOKE PASS: ansiburr Application served through burrmcp.mount end-to-end.")
        return 0
    print("\nSMOKE FAIL: terminal status not reached.")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
