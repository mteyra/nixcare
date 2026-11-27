# Changelog

Full notes for each version are on the [releases page](https://github.com/mteyra/nixcare/releases).

## Unreleased

- Screen-time labels: `laptop-report --json` now also has `labels` (one flat list where every active second has exactly one label: sites from the new `track.siteLabels` option, `Claude Code` / `<terminal>-shell` for terminals, otherwise the app name) and `hourly` (active seconds per local clock hour).
- The tracking collector and `laptop-report` now live in their own Python files (`modules/laptop_telemetry.py`, `modules/laptop_report.py`) instead of inside `track.nix`, so the tests run the real code and linters check it. Settings reach them as command-line options. No behavior changes.
- Removed unused imports and variables flagged by linting. No behavior changes.
- README: status badges, clearer note on `nix profile install` for older Nix, changelog link.

## [0.2.1](https://github.com/mteyra/nixcare/releases/tag/v0.2.1) — 2026-11-14

Accurate reports and quieter maintenance: correct report windows and activity attribution, and scheduled heavy work now waits when CPU, memory or I/O is busy, retrying every 15 minutes.

## [0.2.0](https://github.com/mteyra/nixcare/releases/tag/v0.2.0) — 2026-11-14

First usable release: a portable `nixcare` CLI for Linux and macOS, opt-in scheduled care through NixOS and Home Manager, isolated update builds that never activate on their own, and conservative cleanup with protected rollback generations.
