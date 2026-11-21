"""Run scripted tool calls through the public hook offline, then print an application decision trail."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

# Defensive only: keeps OpenTelemetry quiet if an exporter is installed. No model service or CLI is used.
os.environ["OTEL_SDK_DISABLED"] = "true"

from claude_agent_acgs import ACGSHook


def record_note(body: str) -> None:
    """Execute the local demo tool by printing a note."""
    print(f"Executed record_note: {body}")


async def main() -> None:
    # Two scripted PreToolUse events, fed straight to the hook callback; no model or SDK client runs.
    # The loop below honours the hook's permission decision.
    calls = ["The meeting starts at noon.", "The admin password is demo-secret."]
    hook = ACGSHook(grant_allowed=True)
    decisions: list[dict[str, str]] = []
    for index, body in enumerate(calls):
        event: Any = {
            "hook_event_name": "PreToolUse",
            "session_id": "offline-demo",
            "transcript_path": "",
            "cwd": os.getcwd(),
            "tool_use_id": f"note-{index}",
            "tool_name": "record_note",
            "tool_input": {"body": body},
        }
        answer: Any = await hook(event, event["tool_use_id"], {"signal": None})
        output = answer["hookSpecificOutput"]
        decision = output["permissionDecision"]
        decisions.append(
            {"tool_name": "record_note", "verdict": decision, "reason": output["permissionDecisionReason"]}
        )
        if decision == "allow":
            record_note(body)

    print("Audit trail (application decisions):")
    for entry in decisions:
        print(json.dumps(entry, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
