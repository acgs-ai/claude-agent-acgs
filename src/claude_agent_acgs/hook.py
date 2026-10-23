# SPDX-FileCopyrightText: 2026-present ACGS Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""ACGS governance for the Claude Agent SDK: a PreToolUse hook that checks each tool call before it runs."""

from __future__ import annotations

import dataclasses
import logging
import math
from collections.abc import Mapping
from typing import Any, Literal, get_args

from acgs_lite import Constitution
from claude_agent_sdk.types import (
    ClaudeAgentOptions,
    HookContext,
    HookInput,
    HookJSONOutput,
    HookMatcher,
    PreToolUseHookSpecificOutput,
    SyncHookJSONOutput,
)

from claude_agent_acgs._governance import GovernanceDecision, ToolCallFormatter, ToolCallGovernor, Verdict

logger = logging.getLogger(__name__)

OnReview = Literal["deny", "ask"]
"""What happens to a call that only matched review rules: deny it, or have Claude Code ask for approval."""

_EVENT: Literal["PreToolUse"] = "PreToolUse"
_UNKNOWN_TOOL = "<unknown>"
_EVALUATION_FAILED = "ACGS governance could not evaluate this tool call, so it was denied."
_NO_REVIEWER = "No reviewer is configured for this agent, so it was denied."
_ALLOWED = "No ACGS rule blocks this tool call."


def _output(decision: Literal["allow", "deny", "ask"], reason: str) -> SyncHookJSONOutput:
    return SyncHookJSONOutput(
        hookSpecificOutput=PreToolUseHookSpecificOutput(
            hookEventName=_EVENT, permissionDecision=decision, permissionDecisionReason=reason
        )
    )


def _denied(tool_name: str, reason: str) -> SyncHookJSONOutput:
    return _output("deny", f"Tool call '{tool_name}' was not executed. {reason}")


def _approval_requested(decision: GovernanceDecision) -> str:
    rules = ", ".join(decision.rule_ids)
    return f"ACGS rule(s) {rules} require a person to review the tool call '{decision.tool_name}'. Approve this call?"


class ACGSHook:
    """A Claude Agent SDK ``PreToolUse`` hook that checks every tool call against an ACGS constitution.

    The hook answers ``deny`` for calls that match a blocking rule, ``deny`` or ``ask`` (see ``on_review``) for calls
    that only match review rules, and nothing for other calls, so they go through Claude Code's normal permission
    flow. A hook ``deny`` holds in every permission mode, including ``bypassPermissions``. The hook never raises:
    any error while reading or evaluating a call is answered with ``deny``, because Claude Code lets a call through
    when a hook callback fails.

    Example:
        >>> from claude_agent_sdk import ClaudeAgentOptions
        >>> from claude_agent_acgs import ACGSHook
        >>> options = ACGSHook().apply(ClaudeAgentOptions(permission_mode="bypassPermissions"))
        >>> len(options.hooks["PreToolUse"])
        1
    """

    def __init__(
        self,
        constitution: Constitution | None = None,
        *,
        agent_id: str = "claude-agent",
        on_review: OnReview = "deny",
        grant_allowed: bool = False,
        tool_call_formatter: ToolCallFormatter | None = None,
    ) -> None:
        """Create the hook.

        Args:
            constitution: The rules to enforce. Defaults to ``Constitution.default()``.
            agent_id: Agent identifier recorded by ACGS for each validation.
            on_review: ``"deny"`` (default) denies calls that need review. ``"ask"`` answers ``ask``, so Claude Code
                asks for approval: the ``can_use_tool`` callback in the SDK, or the user in an interactive session.
                Without a callback, an SDK session denies the call.
            grant_allowed: If true, calls no rule blocks are answered with ``allow``, which skips Claude Code's
                permission prompt for them. The default leaves those calls to the normal permission flow.
            tool_call_formatter: Optional ``(tool_name, arguments) -> str`` replacing ``format_tool_call``.

        Raises:
            ValueError: If ``on_review`` is invalid.
            TypeError: If ``grant_allowed`` is not a bool.
        """
        if on_review not in get_args(OnReview):
            msg = f"on_review must be one of {get_args(OnReview)}, not {on_review!r}."
            raise ValueError(msg)
        if not isinstance(grant_allowed, bool):
            msg = "grant_allowed must be a bool."
            raise TypeError(msg)
        self._governor = ToolCallGovernor(constitution, agent_id=agent_id, tool_call_formatter=tool_call_formatter)
        self.constitution = self._governor.constitution
        self.agent_id = agent_id
        self.on_review: OnReview = on_review
        self.grant_allowed = grant_allowed

    def __repr__(self) -> str:
        """Describe the hook without its rules."""
        return (
            f"ACGSHook(agent_id={self.agent_id!r}, on_review={self.on_review!r}, "
            f"grant_allowed={self.grant_allowed!r}, constitution={self.constitution.name!r})"
        )

    def evaluate(self, tool_name: str, arguments: Any) -> GovernanceDecision:
        """Evaluate one tool call without running it or asking anyone. Never raises.

        Args:
            tool_name: The tool name as Claude Code reports it, for example ``Bash`` or ``mcp__mail__send_email``.
            arguments: The tool input.

        Returns:
            The governance decision.
        """
        return self._governor.evaluate(tool_name, arguments)

    def matcher(self, *, timeout: float | None = None) -> HookMatcher:
        """Return a ``HookMatcher`` that runs this hook for every tool, including MCP tools and sub-agent calls.

        Args:
            timeout: Optional hook timeout in seconds. Claude Code blocks a call whose ``PreToolUse`` callback
                times out.

        Returns:
            The matcher, to put under ``ClaudeAgentOptions.hooks["PreToolUse"]``.

        Raises:
            ValueError: If ``timeout`` is not a finite number of seconds greater than zero.
        """
        if timeout is not None and (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            msg = "timeout must be a finite number of seconds greater than zero."
            raise ValueError(msg)
        return HookMatcher(matcher=None, hooks=[self], timeout=timeout)

    def apply(self, options: ClaudeAgentOptions, *, timeout: float | None = None) -> ClaudeAgentOptions:
        """Return a copy of ``options`` with this hook added to its ``PreToolUse`` hooks.

        Existing hooks are kept. Claude Code runs all matching hooks and applies the most restrictive decision,
        so another hook cannot turn this hook's ``deny`` into ``allow``.

        Args:
            options: The options to copy. They are not modified.
            timeout: Optional hook timeout in seconds, as in ``matcher``.

        Returns:
            The new options.
        """
        hooks = dict(options.hooks or {})
        hooks[_EVENT] = [self.matcher(timeout=timeout), *hooks.get(_EVENT, [])]
        return dataclasses.replace(options, hooks=hooks)

    async def __call__(self, input_data: HookInput, tool_use_id: str | None, context: HookContext) -> HookJSONOutput:
        """Decide one ``PreToolUse`` call. Never raises; any error denies the call."""
        del tool_use_id, context  # part of the SDK hook signature; the decision depends only on the input
        tool_name = _UNKNOWN_TOOL
        try:
            if not isinstance(input_data, Mapping):
                msg = "The hook input is not a mapping."
                raise TypeError(msg)
            event = input_data.get("hook_event_name")
            if event != _EVENT:
                logger.warning("ACGSHook only decides PreToolUse calls; it was called for %r and did nothing.", event)
                return {}
            raw_name = input_data.get("tool_name")
            if not isinstance(raw_name, str) or not raw_name:
                msg = "The tool name is missing."
                raise TypeError(msg)
            tool_name = raw_name
            arguments = input_data.get("tool_input")
            decision = self._governor.evaluate(tool_name, {} if arguments is None else arguments)
            return self._answer(decision)
        except Exception:
            logger.warning("ACGS could not decide a PreToolUse call; denying it (fail-closed).", exc_info=True)
            return _denied(tool_name, _EVALUATION_FAILED)

    def _answer(self, decision: GovernanceDecision) -> HookJSONOutput:
        if decision.verdict is Verdict.ALLOW:
            return _output("allow", _ALLOWED) if self.grant_allowed else {}
        if decision.verdict is Verdict.REVIEW:
            if self.on_review == "ask":
                return _output("ask", _approval_requested(decision))
            return _denied(decision.tool_name, f"{decision.reason} {_NO_REVIEWER}")
        return _denied(decision.tool_name, decision.reason)
