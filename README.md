# Nixcare

**Know what needs attention. Make Nix maintenance a routine.**

Nixcare checks your Nix installation, explains findings, and helps you prepare updates. Run one command on Linux or macOS, or add it to your NixOS configuration for scheduled checks and store maintenance.

[Website](https://mteyra.github.io/nixcare/) · [Configuration & troubleshooting](docs/options.md) · [Contributing](CONTRIBUTING.md) · [MIT license](LICENSE)

## Quick start

Already have [Nix](https://nixos.org/download/)? Run a local, read-only health check:

```sh
nix run github:mteyra/nixcare -- doctor
```

Keep the command available:

```sh
nix profile add github:mteyra/nixcare
nixcare doctor
nixcare check-updates --flake /path/to/your/config
```

Replace `/path/to/your/config` with a directory containing `flake.nix` and `flake.lock`. Update discovery contacts your configured input sources, compares revisions, and leaves your source lock unchanged. It does not build or activate updates.

The CLI checks the Nix store connection, available disk space, retained generations, and flake input age. Linux adds memory/swap context and memory-pressure readings; systemd hosts get failed-service checks. On NixOS it detects an activated kernel that still needs a reboot. Findings include practical next steps. An old input is a reason to check upstream, not proof that an update exists.

Installing the CLI starts no background services. Use the NixOS or Home Manager setup below for ongoing care.

<details>
<summary>Flakes disabled, older Nix, or a channel-based setup?</summary>

Enable flakes for this one invocation:

```sh
nix --extra-experimental-features 'nix-command flakes' run github:mteyra/nixcare -- doctor
```

Older Nix versions call `nix profile add` **`nix profile install`**. For a traditional installation using your configured `<nixpkgs>`:

```sh
nix-env -if https://github.com/mteyra/nixcare/archive/refs/tags/v0.2.0.tar.gz
nixcare doctor
```

Local diagnostics work without a flake. Input update discovery requires a flake; channel updates and non-NixOS system upgrades are outside its scope.

</details>

## Scheduled care on NixOS

Add the input and module to your existing system flake. This example assumes an existing `configuration.nix`; replace `workstation` with your configuration's name and `alice` with your existing username. Keep your current nixpkgs input and other modules.

```nix
{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    nixcare = {
      url = "github:mteyra/nixcare";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { nixpkgs, nixcare, ... }: {
    nixosConfigurations.workstation = nixpkgs.lib.nixosSystem {
      system = "x86_64-linux"; # or aarch64-linux
      modules = [
        ./configuration.nix
        nixcare.nixosModules.default
        {
          services.nixcare = {
            enable = true;
            user = "alice";
            flakePath = "/etc/nixos";
            hostName = "workstation";
          };
        }
      ];
    };
  };
}
```

Activate and check the setup:

```sh
sudo nixos-rebuild switch --flake /etc/nixos#workstation
sudo systemctl start nixcare-scan.service
nixcare status
systemctl list-timers 'nixcare-*'
```

Your next login picks up the `nixcare` group used to read system reports. Until then, use `sudo nixcare status`.

| Routine | Default behavior |
| --- | --- |
| Health scan | Daily; includes advisory vulnerability matches and available system/hardware diagnostics |
| Input update discovery | Daily; no live lock edits or system activation |
| Store optimisation | Weekly; deduplicates identical store files without deleting generations |
| Generation cleanup | Off; enable it after reviewing the retention policy |
| Full update build | Off; enable it to prepare a candidate weekly |
| Hardware/activity tracking | Off; separate, explicit opt-ins |

Heavy scheduled jobs have low CPU/I/O priority and wait for AC power by default. If the machine is on battery or another maintenance job is running, they retry every **15 minutes**. Ordinary failures stay visible and are not automatically retried. Weekly schedules and their randomized delays remain in place; timers catch up after downtime. [Inspect or cancel a pending retry](docs/options.md#scheduled-maintenance-and-retries).

Nixcare does not automatically tune your CPU, change power profiles, or switch your system to an update.

### Prepare an update, then choose when to switch

```sh
sudo nixcare update           # Build a candidate from an isolated configuration snapshot
nixcare status --json        # Review build result and package changes
sudo nixcare apply-update    # Activate that exact successfully built candidate
```

Apply refuses a stale candidate if the configuration or candidate lock changed after the build. A failed check invalidates the previous candidate. A successful build establishes that the configuration builds; it cannot prove that every application will behave correctly after activation.

Full candidate builds currently require a self-contained flake directory. Flakes that import siblings such as `../shared` can use portable update discovery, followed by their usual rebuild workflow. [Layout requirements and recovery details](docs/options.md#update-verification-and-recovery).

To schedule these candidate builds, add `services.nixcare.update.enable = true;`. Builds can download substantial data and take time. They never activate automatically.

### Reclaim space with a visible policy

```sh
sudo nixcare clean --dry-run
sudo nixcare clean --apply
```

Cleanup keeps the newest **5 generations**, all generations from the last **14 days**, and the selected, running, and booted systems. It then collects unreferenced store paths. User caches are untouched unless you explicitly list cache directories. Preview is the default, including plain `nixcare clean`.

Set `services.nixcare.clean.enable = true;` to run that policy weekly. Deleted generations cannot be rolled back to. [See retention options and recovery guidance](docs/options.md).

## Other Linux distributions and macOS

The portable CLI has flake outputs for **x86_64-linux**, **aarch64-linux**, **x86_64-darwin**, and **aarch64-darwin**. Intel macOS uses a separate pinned nixpkgs 26.05 input because newer nixpkgs removed that platform. Platform outputs are not a guarantee that every hardware sensor is supported.

With Home Manager, import `nixcare.homeManagerModules.default` and add:

```nix
services.nixcare = {
  enable = true;
  flakePath = "/absolute/path/to/your/config"; # optional
};
```

This installs the CLI and schedules daily user-level scans, plus input checks when a flake path is set. Linux uses systemd user timers; macOS uses launchd. It does not need root or run system cleanup. [Full Home Manager setup and report locations](docs/options.md#home-manager).

## Commands you'll use

| Command | Use |
| --- | --- |
| `nixcare doctor` | Portable health check, also available through the NixOS module |
| `nixcare doctor --flake . --json` | Machine-readable local diagnostics |
| `nixcare check-updates --flake .` | Compare upstream inputs without changing the source lock |
| `nixcare optimise --dry-run` | Explain store deduplication; add `--apply` to run it |
| `nixcare status` | NixOS: latest saved results and errors |
| `sudo nixcare scan --fast` | NixOS: system scan without the vulnerability database check |
| `sudo nixcare clean --dry-run` | NixOS: preview retention and opted-in cache cleanup |
| `nixcare report --hours 1` | NixOS: optional hardware/activity telemetry |

On the portable package, plain `nixcare` runs a scan. With the NixOS module, it shows saved maintenance status. [JSON, exit codes, all options, and troubleshooting](docs/options.md).

## Privacy and scope

Reports and optional activity data stay on your machine. Update checks use your configured flake sources and Nix caches; the optional vulnerability scanner downloads advisory data.

Hardware tracking, application/window tracking, and automatic browser-extension installation each require opt-in. The optional dashboard binds to `127.0.0.1:5601` and blocks cross-origin browser requests. **Other local users can access its HTTP reports.** Its SQLite files are root-owned with restrictive permissions. Do not enable activity tracking on a shared machine if that access is unsuitable.

Nixcare is a maintenance assistant, not a security certification or a promise of faster hardware. Vulnerability matches need review, unsupported sensors may be unavailable, and preserving generations is not a replacement for backups.

## Keep it current

```sh
nix profile list
nix profile upgrade nixcare
```

For a declarative installation, update the `nixcare` flake input and rebuild. To pin a release, use `github:mteyra/nixcare/v0.2.0` as the input URL; change that tag deliberately when upgrading. [Disable, uninstall, and rollback instructions](docs/options.md#disable-or-remove).

## New to Nix?

Nix describes software and system configuration in code, making a machine easier to reproduce and roll back. It still needs maintenance: dependencies age, old generations occupy disk space, and some updates fail to build.

Nixcare turns those concerns into repeatable checks and explicit decisions. The project combines Python diagnostics, declarative Nix modules, background scheduling, conservative cleanup, and regression tests for update and rollback behavior. It is an independent, MIT-licensed community project.

Want to help make it useful on more machines? Read [CONTRIBUTING.md](CONTRIBUTING.md).
