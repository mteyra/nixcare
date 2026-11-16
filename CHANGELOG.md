# Changelog

Full notes for each version are on the [releases page](https://github.com/mteyra/nixcare/releases).

## Unreleased

- Removed unused imports and variables flagged by linting. No behavior changes.
- README: status badges, clearer note on `nix profile install` for older Nix, changelog link.

## [0.2.1](https://github.com/mteyra/nixcare/releases/tag/v0.2.1) — 2026-11-14

Accurate reports and quieter maintenance: correct report windows and activity attribution, and scheduled heavy work now waits when CPU, memory or I/O is busy, retrying every 15 minutes.

## [0.2.0](https://github.com/mteyra/nixcare/releases/tag/v0.2.0) — 2026-11-14

First usable release: a portable `nixcare` CLI for Linux and macOS, opt-in scheduled care through NixOS and Home Manager, isolated update builds that never activate on their own, and conservative cleanup with protected rollback generations.
