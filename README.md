# nixcare

**Declarative NixOS system health scanner, policy-driven cleaner, safe build-only update checker, and 100% local hardware-potential & screen-time tracker.**

`nixcare` packages five integrated workflows into a single NixOS module (`services.nixcare.enable = true;`) and CLI (`nixcare`):

1. **`nixcare scan`** *(daily, read-only)* — Writes a structured JSON health & security report (`/var/lib/nixcare/scan-latest.json`) covering:
   - **Freshness & CVEs**: Age in days of every pin in `flake.lock` + `vulnix` vulnerability scan.
   - **Disk**: `/nix/store` size, generation count, Btrfs usage, and `~/.cache` directories tagged `regenerable` vs. `review` with age in days.
   - **Hardware**: NVMe SMART wear %, battery capacity trend, peak temperatures, MSR thermal/power throttle counters, and kernel PSI stalls.
   - **System**: Failed `systemd` system/user units, journal error count (`-p err`), and firmware update checks (`fwupdmgr`).
   - **Security**: Checks `agent-bridge` mode (`off` / `auth` / `open`), verifies telemetry database permissions (`0600`), and lists any non-loopback listening network ports.
   - **Trackers**: Verifies that `laptop-telemetry`, `aw-server-rust`, and `awatcher` are active and recording.

2. **`nixcare clean`** *(weekly, policy-driven)* — Safe automated housekeeping:
   - Keeps the **last 5 generations** *plus* any generation from the **last 14 days**.
   - Runs `nix store optimise` to deduplicate identical store files via hardlinks.
   - Caps systemd journal size (`500M`) and schedules a monthly Btrfs scrub (`services.btrfs.autoScrub`).
   - Prunes only explicitly allowlisted `regenerable` caches (`rattler`, `uv`, `pip`, `ZapZap`) untouched for `30+` days.

3. **`nixcare update`** *(weekly, build-only)* — Zero-risk update dry-run:
   - Uses an isolated `git worktree` (`/var/cache/nixcare/worktree`) so your live `/etc/nixos` working tree is never dirtied.
   - Updates flake inputs, builds the new system without activating it, checks whether upstream `nixos-26.11` has branched, compares package closures (`nvd diff`), and sends a desktop notification:
     - `✅ Builds fine` — summarizes package updates.
     - `⚠️ Package broken` — identifies the failing derivation while leaving your current lock untouched.
     - `🔒 Security-relevant update` — flags browser, kernel, `openssl`, `systemd`, or `vulnix` updates.
   - Switch with one command (`nixcare apply-update`) or roll back with one command (`nixcare rollback`).

4. **`nixcare track`** *(continuous, 5s interval, 100% local)* — Tracks hardware potential utilization (CPU true compute ceiling %, single-thread bottleneck %, Intel GPU 3D/render & VA-API video decode %, RAM/zram, NVMe I/O, RAPL watts, temps) joined with active window & browser tab focus (Firefox & Chrome via ActivityWatch). Stored with `0600` owner-only permissions and a 90-day rolling retention policy.

5. **`nixcare report`** *(CLI & local web dashboard)* — Terminal report (`nixcare report` / `laptop-report`) and live localhost dashboard on `http://127.0.0.1:5601`.

---

## Quick Start (Flake)

Add `nixcare` to your `/etc/nixos/flake.nix`:

```nix
{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    nixcare = {
      url = "github:mteyra/nixcare";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { self, nixpkgs, nixcare, ... }: {
    nixosConfigurations.X1 = nixpkgs.lib.nixosSystem {
      system = "x86_64-linux";
      modules = [
        nixcare.nixosModules.default
        {
          services.nixcare = {
            enable = true;
            user = "haunted";
            flakePath = "/etc/nixos";
            hostName = "X1";
            agentBridge.mode = "auth"; # "auth" (password once per session), "open", or "off"
          };
        }
      ];
    };
  };
}
```

## CLI Usage

```bash
nixcare scan              # Run read-only system & security scan (outputs JSON + summary)
nixcare report            # Show 24h Hardware Potential + App & Browser Tab Screen Time
nixcare report --hours 1  # Show last 1 hour
nixcare clean --dry-run   # Preview generation & cache cleanup policy without deleting
nixcare clean             # Run policy-based cleanup
nixcare update            # Build-only weekly update check in isolated worktree + diff
nixcare apply-update      # Apply the last verified update from nixcare update
nixcare rollback          # Roll back to the previous NixOS generation
```

## License

MIT
