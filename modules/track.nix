{ config, pkgs, lib, ... }:

let
  cfg = config.services.nixcare;

  awatcherStart = pkgs.writeShellScript "awatcher-hyprland-start" ''
    set -u
    export XDG_RUNTIME_DIR="/run/user/$(id -u)"
    for _ in $(seq 1 30); do
      wd=$(ls "$XDG_RUNTIME_DIR"/wayland-* 2>/dev/null | grep -v '\.lock$' | head -n1 || true)
      hs=$(ls -d "$XDG_RUNTIME_DIR"/hypr/* 2>/dev/null | head -n1 || true)
      [ -n "$wd" ] && [ -n "$hs" ] && break
      sleep 1
    done
    [ -n "$wd" ] && export WAYLAND_DISPLAY="$(basename "$wd")"
    [ -n "$hs" ] && export HYPRLAND_INSTANCE_SIGNATURE="$(basename "$hs")"
    for _ in $(seq 1 20); do
      ${pkgs.curl}/bin/curl -fsS http://127.0.0.1:5600/api/0/info >/dev/null 2>&1 && break
      sleep 0.5
    done
    exec ${pkgs.awatcher}/bin/awatcher --idle-timeout 180
  '';

  # The collector and laptop-report are ordinary Python files next to this
  # module, so the tests and linters check them. They import their helper
  # modules from the same directory.
  trackerDir = pkgs.runCommand "nixcare-tracker" { } ''
    install -Dm644 ${./laptop_telemetry.py} $out/laptop_telemetry.py
    install -Dm644 ${./laptop_report.py} $out/laptop_report.py
    install -Dm644 ${./activity_intersector.py} $out/activity_intersector.py
    install -Dm644 ${./tracking_support.py} $out/tracking_support.py
  '';

  collectorArgs = [
    "--user" cfg.user
    "--retention-days" (toString cfg.track.retentionDays)
    "--site-labels" (builtins.toJSON cfg.track.siteLabels)
    "--nvme" "${pkgs.nvme-cli}/bin/nvme"
  ] ++ lib.optional cfg.track.activity.enable "--activity";

  laptopReportBin = pkgs.writeShellScriptBin "laptop-report" ''
    exec ${pkgs.python3}/bin/python3 ${trackerDir}/laptop_report.py \
      --retention-days ${toString cfg.track.retentionDays} "$@"
  '';
in
{
  config = lib.mkIf (cfg.enable && cfg.track.enable) {
    systemd.user.services.aw-server-rust = lib.mkIf cfg.track.activity.enable {
      description = "ActivityWatch local time-tracking server (Rust)";
      wantedBy = [ "default.target" ];
      unitConfig.ConditionUser = cfg.user;
      serviceConfig = {
        ExecStart = "${pkgs.aw-server-rust}/bin/aw-server --host 127.0.0.1 --port 5600";
        Restart = "always";
        RestartSec = 2;
        UMask = "0077";
      };
    };

    systemd.user.services.awatcher = lib.mkIf cfg.track.activity.enable {
      description = "ActivityWatch Wayland/Hyprland window and idle watcher";
      after = [ "aw-server-rust.service" ];
      wants = [ "aw-server-rust.service" ];
      wantedBy = [ "default.target" ];
      unitConfig.ConditionUser = cfg.user;
      serviceConfig = {
        ExecStart = awatcherStart;
        Restart = "always";
        RestartSec = 3;
      };
    };

    programs.firefox.policies = lib.mkIf cfg.track.browserExtensions.enable {
      ExtensionSettings = {
        "{ef87d84c-2127-493f-b952-5b4e744245bc}" = {
          installation_mode = "force_installed";
          install_url = "https://addons.mozilla.org/firefox/downloads/latest/aw-watcher-web/latest.xpi";
        };
      };
      "3rdparty".Extensions."{ef87d84c-2127-493f-b952-5b4e744245bc}" = {
        consentOfflineDataCollection = true;
      };
    };

    programs.chromium = lib.mkIf cfg.track.browserExtensions.enable {
      # This enables policy files for Chromium/Chrome; it does not install a browser.
      enable = true;
      extensions = [
        "nglaklhklhcoonedhgnpgddginnjdadi;https://clients2.google.com/service/update2/crx"
      ];
    };

    systemd.services.laptop-telemetry = {
      description = "nixcare: Laptop Hardware Potential & Screen-Time Telemetry Daemon (0600 private DB)";
      wantedBy = [ "multi-user.target" ];
      after = [ "network.target" ];
      serviceConfig = {
        ExecStartPre = "-${pkgs.kmod}/bin/modprobe msr";
        ExecStart = "${pkgs.python3}/bin/python3 ${trackerDir}/laptop_telemetry.py ${lib.escapeShellArgs collectorArgs}";
        StateDirectory = "laptop-telemetry";
        StateDirectoryMode = "0700";
        UMask = "0077";
        Restart = "always";
        RestartSec = 2;
        Nice = 10;
        IOSchedulingClass = "idle";
        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ProtectHome = "read-only";
        ReadWritePaths = [ "/var/lib/laptop-telemetry" ];
        RestrictAddressFamilies = [ "AF_UNIX" "AF_INET" ];
      };
    };

    environment.systemPackages = [ laptopReportBin ] ++ lib.optionals cfg.track.activity.enable [
      pkgs.aw-server-rust
      pkgs.awatcher
    ];
  };
}
