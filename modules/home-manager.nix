{ config, pkgs, lib, ... }:

let
  cfg = config.services.nixcare;
  nixcare = "${cfg.package}/bin/nixcare";
  stateDir = "${config.xdg.stateHome}/nixcare";
  flakeArgs = lib.optionals (cfg.flakePath != null) [ "--flake" cfg.flakePath ];
  scanArgs = [ nixcare "scan" ] ++ flakeArgs ++ [ "--output" "${stateDir}/scan-latest.json" ];
  updateArgs = [ nixcare "check-updates" ] ++ flakeArgs ++ [ "--output" "${stateDir}/check-latest.json" ];
  updatesEnabled = cfg.checkUpdates && cfg.flakePath != null;
  mkService = description: args: {
    Unit.Description = description;
    Service = {
      Type = "oneshot";
      ExecStart = lib.escapeShellArgs args;
      Nice = 10;
      IOSchedulingClass = "idle";
      TimeoutStartSec = "10min";
      UMask = "0077";
      NoNewPrivileges = true;
    };
  };
  mkTimer = calendar: {
    Unit.Description = "Scheduled Nixcare check";
    Timer = {
      OnCalendar = calendar;
      Persistent = true;
      RandomizedDelaySec = "30min";
    };
    Install.WantedBy = [ "timers.target" ];
  };
  mkAgent = args: hour: minute: {
    enable = true;
    config = {
      ProgramArguments = args;
      RunAtLoad = true;
      StartCalendarInterval = [ { Hour = hour; Minute = minute; } ];
      ProcessType = "Background";
      LowPriorityIO = true;
    };
  };
in {
  options.services.nixcare = {
    enable = lib.mkEnableOption "daily, local Nix health and upstream input checks";
    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.callPackage ../package.nix { };
      defaultText = lib.literalExpression "pkgs.callPackage nixcare/package.nix { }";
      description = "Portable Nixcare package.";
    };
    flakePath = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "/home/alice/nixos";
      description = "Absolute flake directory to check. Null disables upstream update checks.";
    };
    checkUpdates = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Check upstream inputs daily when flakePath is set. Does not build or activate updates.";
    };
  };
  config = lib.mkIf cfg.enable (lib.mkMerge [
    {
      assertions = [
        {
          assertion = cfg.flakePath == null || lib.hasPrefix "/" cfg.flakePath;
          message = "services.nixcare.flakePath must be an absolute local path.";
        }
      ];
      home.packages = [ cfg.package ];
    }
    (lib.mkIf pkgs.stdenv.hostPlatform.isLinux {
      systemd.user.services.nixcare-scan = mkService "Nixcare local health scan" scanArgs;
      systemd.user.timers.nixcare-scan = mkTimer "*-*-* 14:05:00";
      systemd.user.services.nixcare-check = lib.mkIf updatesEnabled
        (mkService "Nixcare upstream input check" updateArgs);
      systemd.user.timers.nixcare-check = lib.mkIf updatesEnabled (mkTimer "*-*-* 14:20:00");
    })
    (lib.mkIf pkgs.stdenv.hostPlatform.isDarwin {
      launchd.agents.nixcare-scan = mkAgent scanArgs 14 5;
      launchd.agents.nixcare-check = lib.mkIf updatesEnabled (mkAgent updateArgs 14 20);
    })
  ]);
}
