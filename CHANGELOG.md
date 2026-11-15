# Changelog

All notable changes to this project are documented in this file.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/). Entries for new releases are generated with `cz bump`.

## [Unreleased]

## 0.2.0 (unreleased)

### Added

- Offline `examples/deny_and_audit.py`: two scripted `PreToolUse` events fed to the `ACGSHook` callback (one
  allowed, one denied), with an application decision trail.
- Compatibility tests for nested JSON-native arguments and bytes/bytearray formatting with acgs-lite 4.0.0.

### Changed

- Document the compatibility matrix: acgs-lite 4.0.0 is tested in the locked environment, while the CI `test-min`
  job tests the 3.0.1 floor. Runtime support remains `acgs-lite>=3.0.1,<5`.
- Clarify that the adapter validates formatted text and does not issue signed receipts.

## 0.1.0 (unreleased)

### Added

- `ACGSHook`: a Claude Agent SDK `PreToolUse` hook that checks each tool call's name and input against an ACGS
  constitution before the tool runs, for built-in tools, MCP tools and sub-agent tool calls. It answers `deny` for
  blocked calls, which holds in every permission mode, and never raises (fail-closed).
- Review handling: `on_review="deny"` (default), or `on_review="ask"`, which hands the call to `can_use_tool` or
  the user.
- `grant_allowed=`, `tool_call_formatter=` and `timeout=` options; `ACGSHook.apply`, `ACGSHook.matcher` and
  `ACGSHook.evaluate`.
