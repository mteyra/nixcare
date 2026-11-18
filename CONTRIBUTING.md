# Contributing to Nixcare

Useful contributions make maintenance easier to understand and safer to repeat: a reproducible bug report, a tested platform improvement, clearer diagnostics, or a small fix with evidence.

## Run locally

```sh
git clone https://github.com/mteyra/nixcare.git
cd nixcare
nix run . -- doctor
nix flake check --print-build-logs
```

For a quick Python-only regression run:

```sh
python3 -m unittest discover -s test -p 'test_*.py' -v
```

The Python tests use fixtures, temporary directories, mocked administrative commands, and a temporary loopback HTTP server. They must not clean the developer's real store, change system generations, modify `/etc/nixos`, or activate a system.

Run the separate tests against real Nix and Git with:

```sh
NIXCARE_INTEGRATION=1 python3 -m unittest discover -s test -p 'test_portable_integration.py' -v
```

These require a writable Nix store and are skipped in the default sandboxed unit checks. Their temporary fixtures verify changed-input detection, unchanged source locks, and Git subdirectory flakes with sibling inputs.

## Where things live

| Path | Responsibility |
| --- | --- |
| `cli/nixcare.py` | Portable diagnostics, upstream input comparison, explicit store optimisation |
| `package.nix`, `flake.nix`, `default.nix` | Packaging, platform outputs, checks, legacy installation |
| `modules/default.nix` | NixOS options, CLI wrapper, maintenance services and timers |
| `modules/nixcare_core.py` | System scans, retention planning, verified candidate build/apply, saved results |
| `modules/home-manager.nix` | User-level Linux/macOS schedules |
| `modules/track.nix` | Optional tracking: services, ActivityWatch, browser policies, and the settings passed to the collector |
| `modules/laptop_telemetry.py` | The collector: samples hardware and activity into a private SQLite database and serves the local dashboard |
| `modules/laptop_report.py` | `laptop-report`, the terminal summary of the dashboard data |
| `modules/activity_intersector.py` | Clipped, non-overlapping screen-time accounting |
| `modules/tracking_support.py` | Local HTTP request boundaries and bounded summary caching |
| `modules/schedule_guard.py` | Small Linux resource checks before scheduled heavy work |
| `test/` | Behavior and regression tests |
| `website/` | Static public website, deployed by GitHub Pages |

## What a good change preserves

- Local diagnostics work without root and tolerate absent optional platform tools.
- Errors, skipped checks, and unavailable evidence stay visible. A failed check must never be presented as “current” or healthy.
- Cleanup previews by default. It preserves selected, running, booted, recent, and policy-retained generations.
- Candidate preparation never edits the live configuration. Applying verifies and activates the tested closure, rejects stale state, and records recovery failures.
- System mutation is explicit or follows an explicitly enabled module policy.
- Hardware and activity tracking stay optional. Never include real browsing history, personal reports, credentials, or machine configuration in test fixtures or website assets.
- Human output explains an actionable finding. JSON remains parseable, versioned, and free of progress text.

Add a regression test for a meaningful bug or behavior change. Prefer tests that assert externally useful behavior—for example, a changed source refuses activation—over tests that repeat individual implementation statements.

For module changes, evaluate a minimal configuration with tracking disabled as well as the feature you changed. Test Linux and macOS packaging when you change portability. Clearly distinguish evaluated outputs from builds and runtime checks on actual hardware.

The opt-in browser-policy regression check evaluates the actual NixOS module:

```sh
nix eval --impure --file test/nixos-module.nix
```

CI checks the package and regressions on Linux and macOS and evaluates a minimal NixOS module. Passing CI does not replace reviewing the cleanup policy or testing an activation path in a disposable NixOS VM.

## Website changes

The site is plain HTML, CSS, and JavaScript with no build step:

```sh
python3 -m http.server 8080 --directory website
```

Open <http://localhost:8080>, check a narrow mobile viewport, keyboard navigation, installation tabs, copy buttons, and links. Keep sample reports labeled as illustrative. Installation commands and default behavior must agree with the current module and README.

## Report a problem or open a pull request

Use [GitHub issues](https://github.com/mteyra/nixcare/issues) for bugs and requests. Include the command, expected result, actual result, OS/architecture, Nix version, Nixcare version, and relevant redacted output. State whether you installed the portable package, NixOS module, or Home Manager module.

For a pull request, explain the concrete problem and resulting behavior, include relevant validation, and mention any compatibility or migration impact. Keep unrelated system configuration and generated reports out of the diff.

Contributions are distributed under the project's [MIT license](LICENSE).
