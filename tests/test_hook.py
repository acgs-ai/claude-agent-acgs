# SPDX-FileCopyrightText: 2026-present ACGS Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""ACGSHook through the SDK's real control protocol, with ``FakeCLI`` standing in for the Claude Code CLI."""

from __future__ import annotations

import logging
import math
from typing import Any

import pytest
from acgs_lite import Constitution
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, HookMatcher, query
from claude_agent_sdk.types import HookContext, HookInput, HookJSONOutput

from claude_agent_acgs import ACGSHook, Verdict
from tests.fakes import ECHO, LEAK, PUSH, RM_RF, FakeCLI, Outcome, ToolCall, coding_rules

CONTEXT: HookContext = {"signal": None}


async def run(hook: ACGSHook, *calls: ToolCall, options: ClaudeAgentOptions | None = None) -> list[Outcome]:
    cli = FakeCLI(list(calls))
    async for _ in query(prompt="Go.", options=hook.apply(options or ClaudeAgentOptions()), transport=cli):
        pass
    return cli.outcomes


def pre_tool_use(tool_name: Any = "Bash", tool_input: Any = None, **extra: Any) -> HookInput:
    data: dict[str, Any] = {
        "session_id": "s",
        "transcript_path": "t",
        "cwd": "/",
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": ECHO if tool_input is None else tool_input,
        "tool_use_id": "toolu_1",
    }
    return data | extra  # type: ignore[return-value]


def decision_of(output: HookJSONOutput) -> tuple[str | None, str | None]:
    specific: Any = output.get("hookSpecificOutput")
    if specific is None:
        return None, None
    return specific["permissionDecision"], specific["permissionDecisionReason"]


# --- decisions -------------------------------------------------------------------------------------------------------


async def test_blocking_rule_denies() -> None:
    [outcome] = await run(ACGSHook(coding_rules()), ToolCall("Bash", RM_RF))
    assert outcome.decision == "deny"
    assert (
        outcome.reason
        == "Tool call 'Bash' was not executed. Blocked by ACGS rule NO-RM-RF (No recursive force deletes)."
    )
    assert not outcome.ran


async def test_allowed_call_gets_no_decision_by_default() -> None:
    [outcome] = await run(ACGSHook(coding_rules()), ToolCall("Bash", ECHO))
    assert outcome.answers == [{}]
    assert outcome.decision is None
    assert outcome.ran


async def test_grant_allowed_answers_allow() -> None:
    [allowed, denied] = await run(
        ACGSHook(coding_rules(), grant_allowed=True), ToolCall("Bash", ECHO), ToolCall("Bash", RM_RF)
    )
    assert (allowed.decision, allowed.reason) == ("allow", "No ACGS rule blocks this tool call.")
    assert denied.decision == "deny"


async def test_review_denies_by_default() -> None:
    [outcome] = await run(ACGSHook(coding_rules()), ToolCall("Bash", PUSH))
    assert outcome.decision == "deny"
    assert outcome.reason is not None
    assert "PUSH-REVIEW" in outcome.reason
    assert outcome.reason.endswith("No reviewer is configured for this agent, so it was denied.")


async def test_review_asks_when_configured() -> None:
    [outcome] = await run(ACGSHook(coding_rules(), on_review="ask"), ToolCall("Bash", PUSH))
    assert outcome.decision == "ask"
    assert (
        outcome.reason == "ACGS rule(s) PUSH-REVIEW require a person to review the tool call 'Bash'. Approve this call?"
    )


async def test_block_beats_review_in_one_call() -> None:
    both = {"command": "rm -rf build && git push", "description": "x"}
    [outcome] = await run(ACGSHook(coding_rules(), on_review="ask"), ToolCall("Bash", both))
    assert outcome.decision == "deny"


@pytest.mark.parametrize("mode", ["default", "acceptEdits", "plan", "dontAsk", "bypassPermissions", "auto"])
async def test_the_permission_mode_does_not_change_the_answer(mode: str) -> None:
    [outcome] = await run(ACGSHook(coding_rules()), ToolCall("Bash", RM_RF, permission_mode=mode))
    assert outcome.decision == "deny"


async def test_client_session_uses_the_same_hook() -> None:
    cli = FakeCLI([ToolCall("Bash", RM_RF), ToolCall("Bash", ECHO)])
    options = ACGSHook(coding_rules()).apply(ClaudeAgentOptions(permission_mode="bypassPermissions"))
    async with ClaudeSDKClient(options=options, transport=cli) as client:
        await client.query("Go.")
        async for _ in client.receive_response():
            pass
    assert [o.decision for o in cli.outcomes] == ["deny", None]


# --- tools Claude Code adds ----------------------------------------------------------------------------------------


async def test_mcp_tool_with_sensitive_data_is_denied_by_default_rules() -> None:
    [outcome] = await run(ACGSHook(), ToolCall("mcp__mail__send_email", LEAK))
    assert outcome.decision == "deny"
    assert outcome.reason is not None
    assert "ACGS-006" in outcome.reason


async def test_mcp_tool_names_can_be_targeted_by_rules() -> None:
    rules = Constitution.from_yaml_str(
        "name: mail\nrules:\n  - id: NO-MAIL\n    text: No outgoing mail\n    severity: critical\n"
        "    keywords: [mcp mail send email]\n"
    )
    [mail, other] = await run(
        ACGSHook(rules), ToolCall("mcp__mail__send_email", {"to": "a@example.com"}), ToolCall("mcp__mail__list", {})
    )
    assert mail.decision == "deny"
    assert other.decision is None


async def test_sub_agent_delegation_and_its_tool_calls_are_checked() -> None:
    delegation = ToolCall(
        "Agent", {"description": "Clean", "prompt": "Run rm -rf / please", "subagent_type": "general-purpose"}
    )
    inner = ToolCall("Bash", RM_RF, agent_id="agent-1", agent_type="general-purpose")
    outcomes = await run(ACGSHook(coding_rules()), delegation, inner)
    assert [o.decision for o in outcomes] == ["deny", "deny"]


# --- other hooks ---------------------------------------------------------------------------------------------------


async def allow_everything(input_data: HookInput, tool_use_id: str | None, context: HookContext) -> HookJSONOutput:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}


async def crash(input_data: HookInput, tool_use_id: str | None, context: HookContext) -> HookJSONOutput:
    raise RuntimeError("other hook failed")


async def test_apply_keeps_existing_hooks_and_does_not_modify_options() -> None:
    other = HookMatcher(matcher="Bash", hooks=[allow_everything])
    post = HookMatcher(hooks=[allow_everything])
    options = ClaudeAgentOptions(hooks={"PreToolUse": [other], "PostToolUse": [post]})
    hook = ACGSHook()
    applied = hook.apply(options, timeout=5)
    assert applied is not options
    assert options.hooks == {"PreToolUse": [other], "PostToolUse": [post]}
    assert applied.hooks is not None
    [ours, theirs] = applied.hooks["PreToolUse"]
    assert (ours.matcher, ours.hooks, ours.timeout) == (None, [hook], 5)
    assert theirs is other
    assert applied.hooks["PostToolUse"] == [post]


async def test_another_hooks_allow_cannot_override_the_deny() -> None:
    options = ClaudeAgentOptions(hooks={"PreToolUse": [HookMatcher(hooks=[allow_everything])]})
    [outcome] = await run(ACGSHook(coding_rules()), ToolCall("Bash", RM_RF), options=options)
    assert len(outcome.answers) == 2
    assert outcome.decision == "deny"


async def test_another_hook_failing_does_not_stop_the_deny() -> None:
    cli = FakeCLI([ToolCall("Bash", RM_RF)])
    options = ACGSHook(coding_rules()).apply(ClaudeAgentOptions(hooks={"PreToolUse": [HookMatcher(hooks=[crash])]}))
    async for _ in query(prompt="Go.", options=options, transport=cli):
        pass
    assert cli.errors == ["other hook failed"]
    assert cli.outcomes[0].decision == "deny"


# --- fail-closed and malformed input ----------------------------------------------------------------------------------


async def test_unexpected_hook_input_is_denied(caplog: pytest.LogCaptureFixture) -> None:
    hook = ACGSHook()
    with caplog.at_level(logging.WARNING, logger="claude_agent_acgs.hook"):
        output = await hook(["not", "a", "mapping"], None, CONTEXT)  # type: ignore[arg-type]
    assert decision_of(output) == (
        "deny",
        "Tool call '<unknown>' was not executed. ACGS governance could not evaluate this tool call, so it was denied.",
    )
    assert "fail-closed" in caplog.text


@pytest.mark.parametrize("tool_name", [None, "", 42, ["Bash"]])
async def test_missing_or_invalid_tool_name_is_denied(tool_name: Any) -> None:
    output = await ACGSHook()(pre_tool_use(tool_name), None, CONTEXT)
    decision, reason = decision_of(output)
    assert decision == "deny"
    assert reason is not None
    assert reason.startswith("Tool call '<unknown>'")


async def test_missing_tool_input_is_checked_as_empty() -> None:
    data: Any = pre_tool_use("mcp__x__ping")
    del data["tool_input"]
    assert await ACGSHook(grant_allowed=True)(data, None, CONTEXT) == decision_output("allow")


def decision_output(decision: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": "No ACGS rule blocks this tool call.",
        }
    }


@pytest.mark.parametrize("tool_input", ["The password is hunter2", ["The password is hunter2"], {"a": {"b": LEAK}}])
async def test_unusual_tool_input_shapes_are_still_checked(tool_input: Any) -> None:
    decision, _ = decision_of(await ACGSHook()(pre_tool_use("Write", tool_input), None, CONTEXT))
    assert decision == "deny"


async def test_too_deeply_nested_input_is_denied() -> None:
    nested: Any = "x"
    for _ in range(100):
        nested = {"n": nested}
    decision, reason = decision_of(await ACGSHook()(pre_tool_use("Write", nested), None, CONTEXT))
    assert decision == "deny"
    assert reason is not None
    assert "Tool call 'Write'" in reason


async def test_a_failing_formatter_denies() -> None:
    def formatter(tool_name: str, arguments: Any) -> str:
        raise RuntimeError("formatter failed")

    [outcome] = await run(ACGSHook(tool_call_formatter=formatter), ToolCall("Bash", ECHO))
    assert outcome.decision == "deny"


async def test_an_unexpected_error_denies(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    hook = ACGSHook()

    def broken(tool_name: str, arguments: Any) -> Any:
        raise RuntimeError("bug")

    monkeypatch.setattr(hook._governor, "evaluate", broken)
    with caplog.at_level(logging.WARNING, logger="claude_agent_acgs.hook"):
        [outcome] = await run(hook, ToolCall("Bash", ECHO))
    assert outcome.decision == "deny"
    assert outcome.reason == (
        "Tool call 'Bash' was not executed. ACGS governance could not evaluate this tool call, so it was denied."
    )
    assert "bug" in caplog.text


async def test_other_events_are_left_alone(caplog: pytest.LogCaptureFixture) -> None:
    data: Any = pre_tool_use(tool_input=RM_RF, hook_event_name="PostToolUse")
    with caplog.at_level(logging.WARNING, logger="claude_agent_acgs.hook"):
        assert await ACGSHook(coding_rules())(data, None, CONTEXT) == {}
    assert "only decides PreToolUse" in caplog.text


async def test_registered_for_another_event_it_does_nothing() -> None:
    hook = ACGSHook(coding_rules())
    cli = FakeCLI([ToolCall("Bash", RM_RF)], hook_input={"hook_event_name": "PostToolUse"})
    async for _ in query(prompt="Go.", options=hook.apply(ClaudeAgentOptions()), transport=cli):
        pass
    assert cli.outcomes[0].decision is None


# --- known limits (documented in the README) ----------------------------------------------------------------------


@pytest.mark.parametrize("command", ["rm -fr /", "rm -r -f /", "find / -delete"])
async def test_limit_keyword_rules_do_not_parse_shell(command: str) -> None:
    """Keyword rules match text. They are not a shell sandbox; use deny rules or sandboxing for that."""
    [outcome] = await run(ACGSHook(coding_rules()), ToolCall("Bash", {"command": command}))
    assert outcome.decision is None


async def test_limit_default_rules_do_not_cover_destructive_shell_commands() -> None:
    outcomes = await run(ACGSHook(), ToolCall("Bash", RM_RF), ToolCall("Bash", PUSH))
    assert [o.decision for o in outcomes] == [None, None]


# --- construction ---------------------------------------------------------------------------------------------------


def test_defaults_and_repr() -> None:
    hook = ACGSHook()
    assert (hook.agent_id, hook.on_review, hook.grant_allowed) == ("claude-agent", "deny", False)
    assert hook.constitution.name == Constitution.default().name
    assert repr(hook) == (
        "ACGSHook(agent_id='claude-agent', on_review='deny', grant_allowed=False, "
        f"constitution={hook.constitution.name!r})"
    )


def test_evaluate_does_not_run_anything() -> None:
    decision = ACGSHook(coding_rules(), agent_id="ci").evaluate("Bash", PUSH)
    assert decision.verdict is Verdict.REVIEW
    assert decision.rule_ids == ("PUSH-REVIEW",)


def test_invalid_on_review() -> None:
    with pytest.raises(ValueError, match="on_review must be one of"):
        ACGSHook(on_review="allow")  # type: ignore[arg-type]


def test_invalid_grant_allowed() -> None:
    with pytest.raises(TypeError, match="grant_allowed must be a bool"):
        ACGSHook(grant_allowed="yes")  # type: ignore[arg-type]


@pytest.mark.parametrize("timeout", [0, -1, math.inf, math.nan, True, "5"])
def test_invalid_timeout(timeout: Any) -> None:
    with pytest.raises(ValueError, match="timeout must be"):
        ACGSHook().matcher(timeout=timeout)


@pytest.mark.parametrize("timeout", [None, 1, 2.5])
def test_matcher_matches_every_tool(timeout: float | None) -> None:
    hook = ACGSHook()
    matcher = hook.matcher(timeout=timeout)
    assert (matcher.matcher, matcher.hooks, matcher.timeout) == (None, [hook], timeout)
