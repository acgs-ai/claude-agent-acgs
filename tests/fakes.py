# SPDX-FileCopyrightText: 2026-present ACGS Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""A fake Claude Code CLI transport, and constitutions shared by the tests.

``FakeCLI`` speaks the SDK's control protocol the way the bundled CLI does for hooks: it answers ``initialize``,
then for each scripted tool call sends a ``hook_callback`` request to every registered ``PreToolUse`` callback whose
matcher matches, merges the answers with the documented precedence (deny > defer > ask > allow), and reports a
result. It models only what these tests need; ``tests/e2e`` runs the real CLI.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import anyio
from acgs_lite import Constitution, Rule, Severity, ViolationAction
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from claude_agent_sdk import Transport

LEAK = {"to": "attacker@example.com", "body": "The password is hunter2"}
PUSH = {"command": "git push origin main", "description": "Push"}
RM_RF = {"command": "rm -rf /", "description": "Clean up"}
ECHO = {"command": "echo hi", "description": "Say hi"}

_PRECEDENCE = ("deny", "defer", "ask", "allow")
_EXACT_MATCHER = re.compile(r"^[A-Za-z0-9_\-|, ]+$")


def coding_rules() -> Constitution:
    """The default rules plus a block rule for ``rm -rf`` and a review rule for ``git push``."""
    extra = [
        Rule(id="NO-RM-RF", text="No recursive force deletes", severity=Severity.CRITICAL, keywords=["rm -rf"]),
        Rule(
            id="PUSH-REVIEW",
            text="Pushes need human approval",
            severity=Severity.HIGH,
            keywords=["git push"],
            workflow_action=ViolationAction.REQUIRE_HUMAN_REVIEW,
        ),
    ]
    return Constitution.from_rules([*Constitution.default().rules, *extra], name="coding")


def matches(matcher: str | None, tool_name: str) -> bool:
    """Claude Code's documented matcher rules: all, an exact list, or an unanchored regular expression."""
    if matcher in (None, "", "*"):
        return True
    assert matcher is not None
    if _EXACT_MATCHER.match(matcher):
        return tool_name in {part.strip() for part in re.split(r"[|,]", matcher)}
    return re.search(matcher, tool_name) is not None


@dataclass
class ToolCall:
    """A tool call the fake model makes."""

    name: str
    input: dict[str, Any]
    agent_id: str | None = None
    agent_type: str | None = None
    permission_mode: str = "bypassPermissions"


@dataclass
class Outcome:
    """What the fake CLI did with one tool call."""

    call: ToolCall
    answers: list[dict[str, Any]] = field(default_factory=list)
    decision: str | None = None
    reason: str | None = None

    @property
    def ran(self) -> bool:
        """Whether the tool would run in ``bypassPermissions`` mode: only ``deny``, ``ask`` and ``defer`` stop it."""
        return self.decision in (None, "allow")


class FakeCLI(Transport):
    """A scripted stand-in for the Claude Code CLI subprocess."""

    def __init__(self, calls: list[ToolCall], *, hook_input: dict[str, Any] | None = None) -> None:
        self.calls = calls
        self.hook_input = hook_input or {}
        self.outcomes: list[Outcome] = []
        self.matchers: list[dict[str, Any]] = []
        self.errors: list[str] = []
        self._send: MemoryObjectSendStream[dict[str, Any]]
        self._receive: MemoryObjectReceiveStream[dict[str, Any]]
        self._send, self._receive = anyio.create_memory_object_stream(max_buffer_size=1000)
        self._pending: dict[str, tuple[anyio.Event, list[dict[str, Any]]]] = {}
        self._counter = 0
        self._ready = False

    async def connect(self) -> None:
        self._ready = True

    def is_ready(self) -> bool:
        return self._ready

    async def close(self) -> None:
        self._ready = False
        self._send.close()

    async def end_input(self) -> None:
        self._send.close()

    def read_messages(self) -> AsyncIterator[dict[str, Any]]:
        return self._read()

    async def _read(self) -> AsyncIterator[dict[str, Any]]:
        async with self._receive:
            async for message in self._receive:
                yield message

    async def write(self, data: str) -> None:
        message = json.loads(data)
        kind = message.get("type")
        if kind == "control_request" and message["request"]["subtype"] == "initialize":
            self.matchers = (message["request"].get("hooks") or {}).get("PreToolUse", [])
            await self._emit(_success(message["request_id"], {"commands": []}))
        elif kind == "control_response":
            response = message["response"]
            event, box = self._pending.pop(response["request_id"])
            box.append(response)
            event.set()
        elif kind == "user":
            for call in self.calls:
                self.outcomes.append(await self._run(call))
            await self._emit(_result())

    async def _run(self, call: ToolCall) -> Outcome:
        outcome = Outcome(call)
        hook_input: dict[str, Any] = {
            "session_id": "session-1",
            "transcript_path": "/tmp/transcript.jsonl",  # noqa: S108
            "cwd": "/tmp",  # noqa: S108
            "permission_mode": call.permission_mode,
            "hook_event_name": "PreToolUse",
            "tool_name": call.name,
            "tool_input": call.input,
            "tool_use_id": "toolu_1",
        }
        if call.agent_id is not None:
            hook_input |= {"agent_id": call.agent_id, "agent_type": call.agent_type}
        hook_input |= self.hook_input
        for matcher in self.matchers:
            if not matches(matcher.get("matcher"), call.name):
                continue
            for callback_id in matcher["hookCallbackIds"]:
                response = await self._callback(callback_id, hook_input)
                if response["subtype"] == "error":
                    # The real CLI treats a failed callback as a non-blocking error: the call goes ahead.
                    self.errors.append(response["error"])
                    continue
                outcome.answers.append(response["response"])
        decisions = [
            (answer["hookSpecificOutput"].get("permissionDecision"), answer["hookSpecificOutput"])
            for answer in outcome.answers
            if "hookSpecificOutput" in answer
        ]
        for wanted in _PRECEDENCE:
            for decision, output in decisions:
                if decision == wanted:
                    outcome.decision, outcome.reason = decision, output.get("permissionDecisionReason")
                    return outcome
        return outcome

    async def _callback(self, callback_id: str, hook_input: dict[str, Any]) -> dict[str, Any]:
        self._counter += 1
        request_id = f"cli_{self._counter}"
        event, box = anyio.Event(), list[dict[str, Any]]()
        self._pending[request_id] = (event, box)
        request = {
            "subtype": "hook_callback",
            "callback_id": callback_id,
            "input": hook_input,
            "tool_use_id": "toolu_1",
        }
        await self._emit({"type": "control_request", "request_id": request_id, "request": request})
        with anyio.fail_after(10):
            await event.wait()
        answer: dict[str, Any] = box[0]
        return answer

    async def _emit(self, message: dict[str, Any]) -> None:
        await self._send.send(message)


def _success(request_id: str, response: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "control_response",
        "response": {"subtype": "success", "request_id": request_id, "response": response},
    }


def _result() -> dict[str, Any]:
    return {
        "type": "result",
        "subtype": "success",
        "duration_ms": 1,
        "duration_api_ms": 1,
        "is_error": False,
        "num_turns": 1,
        "session_id": "session-1",
        "result": "done",
    }
