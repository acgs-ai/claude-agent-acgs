# SPDX-FileCopyrightText: 2026-present ACGS Contributors
#
# SPDX-License-Identifier: Apache-2.0
import claude_agent_acgs


def test_public_api() -> None:
    assert sorted(claude_agent_acgs.__all__) == sorted(
        ["ACGSHook", "GovernanceDecision", "OnReview", "ToolCallFormatter", "Verdict", "format_tool_call"]
    )
    for name in claude_agent_acgs.__all__:
        assert getattr(claude_agent_acgs, name) is not None
