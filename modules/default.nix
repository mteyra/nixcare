{ config, pkgs, lib, ... }:

let
  cfg = config.services.nixcare;
  portable = pkgs.callPackage ../package.nix { };
  escapeExecArgs = lib.concatMapStringsSep " "
    (arg: lib.replaceStrings [ "%" "$" ] [ "%%" "$$" ] (builtins.toJSON arg));
  settings = pkgs.writeText "nixcare-settings.json" (builtins.toJSON {
    flake_path = cfg.flakePath;
    host_name = cfg.hostName;
    user = cfg.user;
    agent_bridge_mode = cfg.agentBridge.mode;
    track_enabled = cfg.track.enable;
    activity_enabled = cfg.track.activity.enable;
    keep_generations = cfg.clean.keepGenerations;
    keep_days = cfg.clean.keepDays;
    cache_max_age_days = cfg.clean.cacheMaxAgeDays;
    cache_allowlist = cfg.clean.caches;
    clean_optimise = cfg.clean.optimise;
    build_max_jobs = cfg.update.maxJobs;
    build_cores = cfg.update.cores;
  });
  nixcareBin = pkgs.writeShellApplication {
    name = "nixcare";
    runtimeInputs = with pkgs; [ python3 config.nix.package git nvd vulnix systemd nvme-cli iproute2 coreutils util-linux libnotify nixos-rebuild ];
    text = ''
      case "''${1:-}" in
        doctor|check-updates|--version)
          exec ${portable}/bin/nixcare "$@"
          ;;
        "")
          set -- status
          ;;
      esac
      export NIXCARE_CONFIG=${settings}
      exec ${pkgs.python3}/bin/python3 ${./nixcare_core.py} "$@"
    '';
  };
  lowPriority = {
    Nice = 19;
    IOSchedulingClass = "idle";
    CPUWeight = 10;
    IOWeight = 10;
  };
  stateService = {
    Type = "oneshot";
    StateDirectory = "nixcare";
    StateDirectoryMode = "0750";
    Group = "nixcare";
    UMask = "0027";
  };
  resourceGuard = lib.optionalString cfg.deferUnderLoad ''
    ${pkgs.python3}/bin/python3 ${./schedule_guard.py}
  '';
  scheduledScan = pkgs.writeShellApplication {
    name = "nixcare-scheduled-scan";
    text = resourceGuard + ''
      exec ${nixcareBin}/bin/nixcare scan
    '';
  };
  scheduledMaintenance = pkgs.writeShellApplication {
    name = "nixcare-scheduled";
    text = resourceGuard + lib.optionalString cfg.onACPower ''
      # A unit condition would skip this week's run entirely while on battery.
      # Exit 75 before starting work so systemd can retry the pending run.
      # Probe errors also exit 1, so only the explicit "no" result means battery.
      ac_status=0
      ac_state=$(${pkgs.systemd}/bin/systemd-ac-power --verbose) || ac_status=$?
      case "$ac_status:$ac_state" in
        0:yes)
          ;;
        1:no)
          echo "nixcare: Waiting for AC power; scheduled maintenance will retry in 15 minutes." >&2
          exit 75
          ;;
        *)
          echo "nixcare: Cannot check AC power (systemd-ac-power exited $ac_status; state '$ac_state')." >&2
          exit 1
          ;;
      esac
    '' + ''
      exec ${nixcareBin}/bin/nixcare "$@"
    '';
  };
  retryDeferred = {
    # Resource pressure, busy locks and battery deferrals precede any work.
    # Other failures need attention, and must not restart a partial operation.
    Restart = "no";
    RestartForceExitStatus = [ 75 ];
    RestartSec = "15min";
  };
  timer = description: dates: delay: {
    inherit description;
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnCalendar = dates;
      Persistent = true;
      RandomizedDelaySec = delay;
    };
  };
in
{
  imports = [ ./track.nix ];

  options.services.nixcare = {
    enable = lib.mkEnableOption "Nix health checks and scheduled maintenance";
    package = lib.mkOption {
      type = lib.types.package;
      readOnly = true;
      default = nixcareBin;
      description = "The configured NixOS nixcare command.";
    };
    user = lib.mkOption {
      type = lib.types.str;
      default = "root";
      description = "Local user receiving notifications and permission to read health reports.";
    };
    flakePath = lib.mkOption {
      type = lib.types.str;
      default = "/etc/nixos";
      description = "Absolute directory of the system flake. Input updates are checked without editing it.";
    };
    hostName = lib.mkOption {
      type = lib.types.str;
      default = config.networking.hostName;
      description = "Name under nixosConfigurations in the system flake.";
    };
    onACPower = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Run scheduled cleanup, optimisation and full update builds only on AC power. Lightweight checks still run on battery.";
    };
    deferUnderLoad = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Defer scheduled heavy maintenance and vulnerability scans when available memory is below 15%, load reaches the logical CPU count, or sustained memory/I/O stalls indicate contention. Checks before starting; does not pause an already running operation.";
    };
    scan = {
      enable = lib.mkOption { type = lib.types.bool; default = true; description = "Run read-only health scans."; };
      dates = lib.mkOption { type = lib.types.str; default = "daily"; description = "Scan calendar schedule."; };
      vulnerabilities = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = "Include a vulnix scan. Results are advisory package-name/version matches, not confirmed exploits.";
      };
    };
    optimise = {
      enable = lib.mkOption { type = lib.types.bool; default = true; description = "Deduplicate identical Nix store files without deleting generations."; };
      dates = lib.mkOption { type = lib.types.str; default = "weekly"; description = "Store optimisation calendar schedule."; };
    };
    clean = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = false;
        description = "Opt in to scheduled generation pruning and collection of unreferenced store paths. Running and booted generations remain protected.";
      };
      dates = lib.mkOption { type = lib.types.str; default = "weekly"; description = "Cleanup calendar schedule."; };
      keepGenerations = lib.mkOption { type = lib.types.ints.positive; default = 5; description = "Number of newest generations to retain, in addition to protected and recent generations."; };
      keepDays = lib.mkOption { type = lib.types.ints.unsigned; default = 14; description = "Retain generations created within this many days."; };
      cacheMaxAgeDays = lib.mkOption { type = lib.types.ints.positive; default = 30; description = "Minimum age of access, modification and metadata changes before pruning an opted-in cache."; };
      caches = lib.mkOption {
        type = lib.types.listOf lib.types.str;
        default = [];
        example = [ "pip" "uv" ];
        description = "Explicit cache directory names under the selected user's ~/.cache to prune. Empty means no user cache deletion.";
      };
      optimise = lib.mkOption { type = lib.types.bool; default = false; description = "Also deduplicate during cleanup; normally handled by the separate optimise timer."; };
      journalMaxUse = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        example = "500M";
        description = "Optional system journal size cap. Null preserves the system's journald policy.";
      };
      btrfsAutoScrub = lib.mkOption {
        type = lib.types.bool;
        default = false;
        description = "Opt in to monthly scrub of configured Btrfs filesystems.";
      };
    };
    update = {
      enable = lib.mkOption { type = lib.types.bool; default = false; description = "Build isolated update candidates on the configured schedule; never activate automatically."; };
      dates = lib.mkOption { type = lib.types.str; default = "weekly"; description = "Full build check calendar schedule."; };
      maxJobs = lib.mkOption { type = lib.types.ints.positive; default = 1; description = "Maximum simultaneous derivation builds during an update check."; };
      cores = lib.mkOption { type = lib.types.ints.positive; default = 2; description = "Maximum build cores requested per derivation during an update check."; };
      check = {
        enable = lib.mkOption { type = lib.types.bool; default = true; description = "Check for newer flake inputs without a build or changing the live lock."; };
        dates = lib.mkOption { type = lib.types.str; default = "daily"; description = "Lightweight input check calendar schedule."; };
      };
    };
    track = {
      enable = lib.mkOption { type = lib.types.bool; default = false; description = "Opt in to a local hardware telemetry database and dashboard."; };
      activity.enable = lib.mkOption { type = lib.types.bool; default = false; description = "Opt in to ActivityWatch window and screen-time collection (supported desktop required)."; };
      browserExtensions.enable = lib.mkOption { type = lib.types.bool; default = false; description = "Opt in to installing browser ActivityWatch extensions for browser-tab tracking."; };
      siteLabels = lib.mkOption {
        type = lib.types.attrsOf lib.types.str;
        default = { "claude.ai" = "claude.ai"; "youtube.com" = "YouTube"; "github.com" = "GitHub"; "studon.fau.de" = "StudOn"; };
        example = { "youtube.com" = "YouTube"; };
        description = "Sites (domain, subdomains included) that get their own screen-time label; every other site counts as its browser.";
      };
      retentionDays = lib.mkOption { type = lib.types.ints.positive; default = 90; description = "Days of optional telemetry retained locally."; };
    };
    agentBridge.mode = lib.mkOption {
      type = lib.types.enum [ "auth" "open" "off" ];
      default = "off";
      description = "Authorization mode for explicitly configured agent-bridge systemd units. Off adds no nixcare polkit authorization.";
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      { assertion = lib.hasPrefix "/" cfg.flakePath; message = "services.nixcare.flakePath must be an absolute directory."; }
      { assertion = cfg.user == "root" || config.users.users.${cfg.user}.isNormalUser; message = "services.nixcare.user must name an existing local user."; }
      { assertion = builtins.all (name: name != "" && name != "." && name != ".." && !(lib.hasInfix "/" name)) cfg.clean.caches; message = "services.nixcare.clean.caches must contain directory names, not paths."; }
      { assertion = !cfg.track.activity.enable || cfg.track.enable; message = "nixcare activity tracking requires services.nixcare.track.enable."; }
      { assertion = !cfg.track.browserExtensions.enable || cfg.track.activity.enable; message = "nixcare browser extensions require services.nixcare.track.activity.enable."; }
    ];
    environment.systemPackages = [ cfg.package pkgs.nvd pkgs.vulnix ];
    users.groups.nixcare = {};
    users.users.${cfg.user}.extraGroups = [ "nixcare" ];
    environment.etc."systemd/journald.conf.d/nixcare.conf" = lib.mkIf (cfg.clean.journalMaxUse != null) {
      text = ''
        [Journal]
        SystemMaxUse=${cfg.clean.journalMaxUse}
      '';
    };
    services.btrfs.autoScrub = lib.mkIf cfg.clean.btrfsAutoScrub {
      enable = lib.mkDefault true;
      interval = lib.mkDefault "monthly";
      fileSystems = lib.mkDefault (builtins.attrNames (lib.filterAttrs (_: fs: fs.fsType == "btrfs") config.fileSystems));
    };
    systemd.services.nixcare-scan = lib.mkIf cfg.scan.enable {
      description = "nixcare: read-only health scan";
      serviceConfig = stateService // lowPriority
        // lib.optionalAttrs (cfg.scan.vulnerabilities && cfg.deferUnderLoad) retryDeferred // {
        ExecStart = if cfg.scan.vulnerabilities
          then lib.getExe scheduledScan
          else "${nixcareBin}/bin/nixcare scan --fast";
        TimeoutStartSec = "15min";
      };
    };
    systemd.timers.nixcare-scan = lib.mkIf cfg.scan.enable (timer "nixcare: health scan" cfg.scan.dates "15m");
    systemd.services.nixcare-check-updates = lib.mkIf cfg.update.check.enable {
      description = "nixcare: check flake inputs without a build";
      environment.HOME = "/var/cache/nixcare";
      serviceConfig = stateService // lowPriority // {
        ExecStopPost = "${pkgs.bash}/bin/bash -c 'test ! -f /var/lib/nixcare/check-updates-latest.json || ${pkgs.coreutils}/bin/chmod 0640 /var/lib/nixcare/check-updates-latest.json'";
        CacheDirectory = "nixcare";
        CacheDirectoryMode = "0750";
        ExecStart = escapeExecArgs [
          "${portable}/bin/nixcare" "check-updates" "--flake" cfg.flakePath
          "--output" "/var/lib/nixcare/check-updates-latest.json"
        ];
        ExecStartPost = "${pkgs.coreutils}/bin/chmod 0640 /var/lib/nixcare/check-updates-latest.json";
        TimeoutStartSec = "20min";
      };
    };
    systemd.timers.nixcare-check-updates = lib.mkIf cfg.update.check.enable (timer "nixcare: daily input freshness" cfg.update.check.dates "30m");
    systemd.services.nixcare-clean = lib.mkIf cfg.clean.enable {
      description = "nixcare: opted-in generation and store cleanup";
      serviceConfig = stateService // lowPriority // retryDeferred // {
        ExecStart = "${lib.getExe scheduledMaintenance} clean --apply";
        TimeoutStartSec = "3h30min";
      };
    };
    systemd.timers.nixcare-clean = lib.mkIf cfg.clean.enable (timer "nixcare: cleanup" cfg.clean.dates "30m");
    systemd.services.nixcare-optimise = lib.mkIf cfg.optimise.enable {
      description = "nixcare: deduplicate identical store files";
      serviceConfig = stateService // lowPriority // retryDeferred // {
        ExecStart = "${lib.getExe scheduledMaintenance} optimise --apply";
        TimeoutStartSec = "2h10min";
      };
    };
    systemd.timers.nixcare-optimise = lib.mkIf cfg.optimise.enable (timer "nixcare: store deduplication" cfg.optimise.dates "2h");
    systemd.services.nixcare-update = lib.mkIf cfg.update.enable {
      description = "nixcare: build an isolated update candidate";
      environment.HOME = "/var/cache/nixcare";
      serviceConfig = stateService // lowPriority // retryDeferred // {
        ExecStart = "${lib.getExe scheduledMaintenance} update";
        CacheDirectory = "nixcare";
        CacheDirectoryMode = "0750";
        TimeoutStartSec = "3h";
      };
    };
    systemd.timers.nixcare-update = lib.mkIf cfg.update.enable (timer "nixcare: verified update build" cfg.update.dates "1h");
  };
}
