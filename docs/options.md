# Configuration and operations

Start with the [README](../README.md). This reference covers the optional automation and the behavior to expect when something fails.

## NixOS options

All options below live under `services.nixcare`. Import `nixcare.nixosModules.default` first. System flake updates require an existing `flake.lock`; the flake's `nixosConfigurations` key must match `hostName`.

| Option | Default | Purpose |
| --- | --- | --- |
| `enable` | `false` | Install the configured NixOS CLI and enable selected routines |
| `user` | `"root"` | Existing local user to receive notifications and join the report-reading `nixcare` group |
| `flakePath` | `"/etc/nixos"` | Absolute local system flake directory |
| `hostName` | `config.networking.hostName` | Configuration key under `nixosConfigurations` |
| `onACPower` | `true` | Wait for AC power before starting scheduled cleanup, optimisation, and full builds |
| `deferUnderLoad` | `true` | Wait for memory/CPU/I/O resources before heavy scheduled jobs and vulnerability scans |
| `scan.enable` | `true` | Enable the system scan timer |
| `scan.dates` | `"daily"` | Scan calendar schedule |
| `scan.vulnerabilities` | `true` | Run vulnix; matches are advisory, failures are reported |
| `optimise.enable` | `true` | Schedule store deduplication |
| `optimise.dates` | `"weekly"` | Deduplication calendar schedule |
| `update.check.enable` | `true` | Schedule lightweight upstream input checks |
| `update.check.dates` | `"daily"` | Input check schedule |
| `update.enable` | `false` | Schedule full candidate builds; no automatic activation |
| `update.dates` | `"weekly"` | Candidate build schedule |
| `update.maxJobs` | `1` | Maximum concurrent Nix build jobs during candidate preparation |
| `update.cores` | `2` | Cores advertised to individual build jobs |
| `clean.enable` | `false` | Schedule generation pruning and garbage collection |
| `clean.dates` | `"weekly"` | Cleanup schedule |
| `clean.keepGenerations` | `5` | Retain this many newest system generations, plus protected/recent ones |
| `clean.keepDays` | `14` | Retain all system generations this recent |
| `clean.caches` | `[]` | Explicit directory names under the configured user's `~/.cache` |
| `clean.cacheMaxAgeDays` | `30` | Minimum cache access/modification/metadata age before removal |
| `clean.optimise` | `false` | Also deduplicate during cleanup; the separate timer usually suffices |
| `clean.journalMaxUse` | `null` | Optional journald size limit, such as `"500M"` |
| `clean.btrfsAutoScrub` | `false` | Enable monthly scrub for configured Btrfs filesystems |
| `track.enable` | `false` | Local hardware sampling and loopback dashboard |
| `track.activity.enable` | `false` | ActivityWatch and application/window tracking |
| `track.browserExtensions.enable` | `false` | Install ActivityWatch browser extensions through browser policies |
| `track.retentionDays` | `90` | Retention for the Nixcare telemetry SQLite database |
| `agentBridge.mode` | `"off"` | Legacy agent-bridge mode recorded in scan reports; do not use it to grant privileges |

`package` is the module's generated, read-only CLI package. Calendar strings use systemd's `OnCalendar` syntax. `onACPower` applies to scheduled heavy jobs; manually invoking a command runs it immediately.

An example of explicitly enabled maintenance:

```nix
services.nixcare = {
  enable = true;
  user = "alice";
  hostName = "workstation";
  clean = {
    enable = true;
    keepGenerations = 8;
    keepDays = 30;
    # Leave caches = [] unless you have reviewed particular cache directories.
  };
  update = {
    enable = true;
    dates = "Sun *-*-* 12:00:00";
    maxJobs = 1;
    cores = 2;
  };
};
```

Nixcare's garbage collection removes unreferenced store paths globally, not just packages from generations it pruned. Other profiles and GC roots continue to protect their referenced packages. Store optimisation shares identical files with hardlinks and does not delete generation history. It may recover little space if the store is already deduplicated.

## Scheduled maintenance and retries

Cleanup, store optimisation, and full candidate builds keep their weekly schedules unless you change their `dates` options. They run with low CPU/I/O priority and check for AC power before starting when `onACPower = true`.

With `deferUnderLoad = true`, heavy scheduled jobs and vulnerability scans also wait while any of these is true: available memory is below **15%** of total, the one-minute load average reaches the number of logical CPUs, or full memory/I/O pressure averages at least **2%/10%** over 60 seconds. Pressure accounting is optional on kernels without PSI. Fast scans and input discovery remain lightweight checks. These are checks before starting, not a guarantee of spare resources throughout a build. Nix daemon builds may consume resources outside the scheduling service's priority controls.

Battery power, resource contention, or another Nixcare maintenance operation returns temporary exit status `75`. Its systemd service retries every **15 minutes**. Ordinary errors, including unreadable resource probes, remain visible and are not automatically retried. Direct CLI commands run immediately; disabling `deferUnderLoad` also bypasses the resource check for scheduled jobs.

Deferred attempts leave the previous saved report unchanged. Check the current service and its journal to see whether work is running or waiting:

```sh
systemctl status nixcare-update.service
journalctl -u nixcare-update.service -n 30 --no-pager
```

Cancel a pending retry by stopping its service:

```sh
sudo systemctl stop nixcare-update.service
```

Use `nixcare-clean.service` or `nixcare-optimise.service` for those jobs. Stopping the service cancels the pending retry; its timer can still start a run at the next scheduled time. To disable future runs, change that routine's `enable` option and rebuild. Stopping an actively running service interrupts its work, so check its status first.

### Keeping a busy desktop session undisturbed

`nixos-rebuild switch` activates a system configuration and may restart changed services. It does not request a reboot, but it is not a promise that a desktop session will be unaffected. Save work and choose a suitable time for activation. `nixos-rebuild boot` prepares the configuration for the next boot without switching the running system or rebooting it.

Health scans and input checks do not activate configurations. Cleanup removes old generations and globally unreferenced store paths; store optimisation deduplicates files. Neither requests a logout or reboot, but both perform disk work. Leave maintenance until later when preserving a busy session is the priority.

## Home Manager

Use this module for daily checks without system-level maintenance. Add `inputs.nixcare.url = "github:mteyra/nixcare";` to the flake that already manages your Home Manager configuration. Include `nixcare` in your `outputs` arguments alongside `nixpkgs` and `home-manager`, then include its module:

```nix
homeManagerConfigurations.alice = home-manager.lib.homeManagerConfiguration {
  pkgs = nixpkgs.legacyPackages.x86_64-linux;
  modules = [
    ./home.nix
    nixcare.homeManagerModules.default
    {
      services.nixcare = {
        enable = true;
        flakePath = "/home/alice/my-config";
      };
    }
  ];
};
```

Keep your existing `home.nix`, username, home directory, state version, and platform. For Apple Silicon use `aarch64-darwin`; for Intel macOS use a nixpkgs version that still supports `x86_64-darwin`. NixOS users normally choose the NixOS module rather than enabling duplicate checks in both modules.

| Home Manager option | Default | Purpose |
| --- | --- | --- |
| `services.nixcare.enable` | `false` | Install the portable CLI and schedule a daily user scan |
| `services.nixcare.flakePath` | `null` | Absolute directory to inspect; `null` disables scheduled upstream checks |
| `services.nixcare.checkUpdates` | `true` | Enable daily upstream checks when a path is configured |
| `services.nixcare.package` | The portable package | Override the installed package if needed |

Activate with your usual `home-manager switch --flake …` command. Linux uses `nixcare-scan.timer` at 14:05 and `nixcare-check.timer` at 14:20, each with up to 30 minutes' randomized delay. Timers are persistent. macOS launch agents use the same nominal times and run when loaded. These are local times.

Home Manager automation runs in your user session. On Linux it requires a systemd user manager; it is not a system-wide daemon that guarantees runs while you are logged out. On macOS it runs as a launch agent in your login session.

Reports are JSON at:

```text
$XDG_STATE_HOME/nixcare/scan-latest.json
$XDG_STATE_HOME/nixcare/check-latest.json
```

`XDG_STATE_HOME` defaults to `~/.local/state`. Portable `--output` writes are atomic and create owner-readable files.

## CLI behavior and JSON

The portable CLI is also available as `nixcare doctor` through the NixOS module.

```sh
nixcare doctor --flake . --json
nixcare doctor --output ~/.local/state/nixcare/manual-scan.json
nixcare check-updates --flake . --json --timeout 300
nixcare check-updates --flake . --fail-on-updates
```

Portable scans return exit `0` for a completed check with no errors, including advisory warnings. `--strict` makes warnings return `1`. Update discovery returns `1` on failure and, with `--fail-on-updates`, `2` when changes are available. Reports include `schema_version`; individual checks distinguish `ok`, `warning`, `error`, and `skipped`. A skipped or failed check is not evidence that the underlying system is healthy.

NixOS adds these administrative commands:

```sh
sudo nixcare scan --json
sudo nixcare scan --fast
nixcare status --json
sudo nixcare clean --dry-run
sudo nixcare clean --apply
sudo nixcare optimise --apply
sudo nixcare update
sudo nixcare apply-update
sudo nixcare rollback
```

`--dry-run` takes precedence over `--apply` for cleanup and optimisation. `clean --skip-optimise` skips the additional cleanup-time deduplication if you enabled it. The NixOS scan has a different system-report schema from the portable doctor. `scan --fast` skips vulnix; it still collects other available system diagnostics.

The NixOS scan returns `partial` and exit `1` if an enabled or required collector fails; `collection_errors` names the gaps, and unavailable counts are `null`. Cache inventory has a shared 10,000-entry/250 ms traversal budget; incomplete sizes are lower bounds and explicitly marked. A complete scan with health findings returns `attention` and exit `0`, so findings and failed collection remain distinct. Interrupted cleanup, optimisation, and update checks record `interrupted` and exit `130`.

`nixcare status` reads saved results under `/var/lib/nixcare/`. It does not start a new scan. The `nixcare` group grants read access to these system reports, which may include service names, listening sockets, and journal excerpts. Add only users who should see that information.

## Update verification and recovery

`check-updates` writes its proposed lock to a temporary location, compares it with the original, then removes it. It preserves the source flake's Git context, including flakes in a repository subdirectory that use sibling inputs such as `../shared`. It does not test builds. Explicit revision pins stay pinned, and local/path inputs follow their own source behavior.

`sudo nixcare update` copies the configuration into `/var/cache/nixcare/worktree`, updates that snapshot's inputs, and builds the selected NixOS system. It saves a package diff and a GC root for the candidate. Source configuration changes during the copy/build cause the candidate to be rejected.

`sudo nixcare apply-update` checks the recorded configuration digest, candidate lock digest, host, and built store path. It copies the verified lock into the live configuration and activates the exact candidate. If activation fails, it attempts to restore the previous lock, next-boot profile, and running system separately, and records any recovery failure. A lock edited concurrently during activation is preserved and reported for review. There is no guarantee of automatic recovery from every application or hardware failure.

The latest pre-activation lock backup is `/var/lib/nixcare/flake.lock.before-update`. An activation can require a reboot to use a new kernel. After activation, run `nixcare doctor` and check your applications.

```sh
sudo nixcare rollback
```

Rollback activates the previous system generation. It **does not change your flake.lock**; review the source configuration before your next rebuild, or it may select the newer packages again. You can also choose a retained generation in the boot menu.

Full candidate builds require a **self-contained system flake directory** and a regular `flake.lock` file, not a per-file symlink. The snapshot contains `flakePath` only: sibling-relative inputs or imports such as `../shared` are not supported after relocation. Use `check-updates` for discovery while preserving that source layout, and your usual NixOS build/activation workflow for such configurations. External mutable files, unusual permissions, and private sources may also require adaptation. A failed build is reported and never counts as an update ready to apply.

## Optional hardware and activity reports

```nix
services.nixcare.track = {
  enable = true;
  # Both of these are separate choices:
  activity.enable = true;
  browserExtensions.enable = true;
  retentionDays = 30;
};
```

For hardware-only sampling, leave both activity options `false`. Activity integration targets Wayland/Hyprland with ActivityWatch; it is not a universal desktop watcher. Browser-extension installation uses policies on your already configured browsers. It does not make Chromium a mandatory installed application.

```sh
nixcare report --hours 1
nixcare report --hours 0.5    # Last 30 minutes
nixcare report --json
```

The dashboard is at <http://127.0.0.1:5601>. The ActivityWatch UI, when enabled, is at <http://127.0.0.1:5600>. The Nixcare API validates loopback Host/Origin headers, escapes displayed activity text, limits query windows to configured retention, and caches repeated summaries briefly.

`--hours` accepts a finite positive number up to the configured retention period. Reports keep the exact requested boundary, exclude future samples, and distinguish hardware coverage from activity coverage. Missing history does not mean zero use. Active time requires idle/active evidence; an open window alone is insufficient. Application and site totals are clipped to active focus intervals, so background tabs do not accumulate focused browsing time.

ActivityWatch queries prefer this machine's buckets. Ambiguous, unavailable, or oversized history falls back to sampled activity with a visible warning and `activity_source`. Each history response is limited to 4 MiB and 20,000 events; database reads have a time budget. Request fewer hours when a detailed window is too large. A fallback is less precise than complete ActivityWatch intervals, particularly around focus changes.

The Nixcare database is at `/var/lib/laptop-telemetry/telemetry.db`, with root-owned `0700` directory and `0600` files. **Loopback HTTP reports are accessible to other local OS users.** ActivityWatch has its own data storage and retention; `track.retentionDays` applies to Nixcare's SQLite samples, not to ActivityWatch's independent history. Disabling tracking stops collection but preserves existing data.

CPU capacity is a frequency-weighted busy estimate, not a benchmark. Application rows report whole-system load while an application is focused, not energy attributable solely to that application. Sensor support varies; some unsupported readings appear as zero. Use measurements as diagnostic context rather than guaranteed hardware limits.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| `experimental Nix feature … is disabled` | Use the one-invocation flags in the README or enable `nix-command flakes` in your Nix configuration |
| A check reports an error while offline | Retry with connectivity; update discovery cannot establish upstream freshness offline |
| A NixOS scan says `partial` | Inspect `collection_errors` in `sudo nixcare scan --json`; restore the collector or explicitly disable an unwanted optional check |
| Old lock input but no available update | It may be intentionally revision-pinned, or upstream may not have changed |
| No saved report yet | Check timers or start the service once; installing the portable CLI alone creates no schedule |
| `Permission denied` reading system reports | Start a new login session after joining `nixcare`; use `sudo nixcare status` meanwhile |
| Heavy job is waiting or exits `75` | Check its journal for power, memory/CPU/I/O pressure, or another maintenance job; deferred jobs retry every 15 minutes |
| Candidate became stale | Finish configuration edits and run `sudo nixcare update` again |
| A snapshot build cannot resolve `../shared` | Use the full-build layout guidance above; portable `check-updates` preserves Git subdirectory context |
| Optional report unavailable | Enable `track.enable` and inspect `laptop-telemetry.service` |
| No browser activity | Check activity opt-in, supported desktop, browser extension, and the ActivityWatch UI |

Useful NixOS diagnostics:

```sh
systemctl list-timers 'nixcare-*'
systemctl status nixcare-scan.service nixcare-update.service
journalctl -u nixcare-scan.service -n 60 --no-pager
journalctl -u nixcare-update.service -n 60 --no-pager
nixcare status --json
```

For Home Manager on Linux, use `systemctl --user list-timers 'nixcare-*'` and `journalctl --user -u nixcare-check.service`. When sharing diagnostics in a GitHub issue, remove usernames, private paths, IP addresses, window titles, browser URLs, and any secrets.

## Disable or remove

For NixOS, set `services.nixcare.enable = false;` and rebuild. To disable just cleanup or candidate builds, set their individual `enable` options to `false`. For Home Manager, disable the service and activate your Home Manager configuration. Existing report and telemetry files are preserved.

To remove a profile-installed CLI:

```sh
nix profile list
nix profile remove nixcare
```

Use the package name shown by `nix profile list` if it differs. A traditional `nix-env` installation can be removed with `nix-env -e nixcare`.

Upgrade a moving flake input declaratively:

```sh
sudo nix flake update nixcare --flake /etc/nixos
sudo nixos-rebuild switch --flake /etc/nixos#workstation
```

Replace the path and configuration name with yours. If pinned to a release tag, first change the input URL to the desired release. Review changes before rebuilding.
