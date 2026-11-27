# Changelog

All notable changes to this project are documented in this file.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/). Entries for new releases are generated with `cz bump`.

## [Unreleased]

### Security

- A control character that Python treats as whitespace (vertical tab, form feed, U+001C to U+001F, NEL) inside
  a tool-call argument value was rendered as a space. Inside a word it split the word in two, so
  `del<VT>ete` did not match a `delete` keyword and the call was allowed. A value that contains control
  characters is now also rendered with them removed and with them as spaces, and rules are matched against
  every rendering. This also applies to identifier values such as `skip<NUL>_audit`.
- **Behaviour change:** a value with more than one of these whitespace control characters is denied, because
  each one could be a separator or sit inside a word. Pass your own `tool_call_formatter` if your tools
  legitimately receive such values.
- Checking whether an argument value is a single identifier no longer takes exponential time on crafted
  values.

Not covered:

- A value with several non-whitespace control characters (for example NUL or ESC) where one sits inside a
  word and another separates words, such as `del<NUL>ete<SOH>everything`. Such values are not denied, because
  text with terminal colour codes contains many of these characters. The printable part of such a code, for
  example `[0m`, also stays in the text.
- Other characters that Python treats as whitespace inside a word, such as no-break space or U+2028.
- Invisible format characters such as zero-width space, which acgs-lite 3.x does not remove.
- Control characters in tool names and argument names, and values whose `str()` escapes control characters
  (for example `bytes`).

## 0.2.0 (2026-11-21)

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
