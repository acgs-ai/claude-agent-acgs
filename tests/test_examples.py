# SPDX-FileCopyrightText: 2026-present ACGS Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""The README only shows code that runs, and the quickstart output it shows is the real output."""

from __future__ import annotations

import re
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import ClaudeAgentOptions, PermissionResultDeny

from claude_agent_acgs import ACGSHook
from tests.fakes import coding_rules

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
QUICKSTART = ROOT / "examples" / "quickstart.py"


def _blocks(language: str) -> list[str]:
    return [textwrap.dedent(block) for block in re.findall(rf"```{language}\n(.*?)```", README, re.DOTALL)]


def test_quickstart_output_matches_readme() -> None:
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-W", "error", str(QUICKSTART)],
        capture_output=True,
        text=True,
        check=True,
        cwd=ROOT,
        timeout=300,
    )
    [expected] = [block for block in _blocks("text") if block.startswith("Bash: deny.")]
    assert completed.stdout == expected
    assert completed.stderr == ""


def test_readme_embeds_the_quickstart_verbatim() -> None:
    assert QUICKSTART.read_text(encoding="utf-8") in _blocks("python")


def test_readme_states_the_supported_versions() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for package in ("claude-agent-sdk", "acgs-lite"):
        [requirement] = re.findall(rf'"({re.escape(package)}>=[^"]+)"', pyproject)
        assert f"`{requirement}`" in README
        [floor] = re.findall(rf'"{re.escape(package)}==([^"]+)"', pyproject)
        assert requirement.startswith(f"{package}>={floor},")
    assert README.count("claude-agent-sdk 0.2.119") == 3


def _hook(options: ClaudeAgentOptions) -> ACGSHook:
    assert options.hooks is not None
    [hook] = [h for m in options.hooks["PreToolUse"] for h in m.hooks if isinstance(h, ACGSHook)]
    return hook


async def test_every_readme_python_block_runs(capsys: pytest.CaptureFixture[str]) -> None:
    blocks = _blocks("python")
    assert len(blocks) == 5
    namespaces: list[dict[str, Any]] = []
    for block in blocks:
        namespace: dict[str, Any] = {"__name__": "readme", "constitution": coding_rules()}
        exec(compile(block, "<README>", "exec", dont_inherit=True), namespace)  # noqa: S102
        namespaces.append(namespace)
    assert "normalized: mcp deploy run job | dry run" in capsys.readouterr().out
    assert _hook(namespaces[0]["options"]).on_review == "deny"
    review = namespaces[3]
    assert _hook(review["options"]).on_review == "ask"
    assert isinstance(await review["reviewer"]("Bash", {}, None), PermissionResultDeny)
    assert review["options"].can_use_tool is review["reviewer"]
    hook = namespaces[4]["hook"]
    assert hook.agent_id == "ops-agent"
    assert hook.evaluate("mcp__deploy__run_job", {"job": "billing"}).rule_ids == ("DEPLOY-REVIEW",)
    assert hook.evaluate("Bash", {"command": "psql prod-db"}).verdict.value == "deny"
