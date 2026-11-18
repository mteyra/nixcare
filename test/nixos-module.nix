# Run with: nix eval --impure --file test/nixos-module.nix
let
  flake = builtins.getFlake (toString ../.);
  lib = flake.inputs.nixpkgs.lib;
  configuration = browserExtensions: extra:
    (lib.nixosSystem {
      system = "x86_64-linux";
      modules = [
        flake.nixosModules.default
        {
          system.stateVersion = "24.11";
          services.nixcare = {
            enable = true;
            flakePath = "/etc/nixos/config's %n/\${HOME}";
            clean.enable = true;
            update.enable = true;
            track = {
              enable = true;
              activity.enable = true;
              browserExtensions.enable = browserExtensions;
            };
          };
        }
        extra
      ];
    }).config;
  enabled = configuration true {};
  disabled = configuration false {};
  fast = configuration false { services.nixcare.scan.vulnerabilities = false; };
  unguarded = configuration false { services.nixcare.deferUnderLoad = false; };
  noActivity = configuration false { services.nixcare.track.activity.enable = lib.mkForce false; };
  collectorExec = config: config.systemd.services.laptop-telemetry.serviceConfig.ExecStart;
  extension = "nglaklhklhcoonedhgnpgddginnjdadi;https://clients2.google.com/service/update2/crx";
  policies = path:
    builtins.fromJSON enabled.environment.etc.${path}.text;
  maintenanceServices = map (action: enabled.systemd.services."nixcare-${action}") [
    "clean" "optimise" "update"
  ];
  retriesOnlyDeferredWork = service:
    service.serviceConfig.Type == "oneshot"
    && service.serviceConfig.Restart == "no"
    && service.serviceConfig.RestartForceExitStatus == [ 75 ]
    && service.serviceConfig.RestartSec == "15min"
    && !(service.unitConfig ? ConditionACPower);
in
assert enabled.programs.chromium.enable;
assert !disabled.programs.chromium.enable;
assert !(disabled.environment.etc ? "opt/chrome/policies/managed/default.json");
assert builtins.elem extension (policies "opt/chrome/policies/managed/default.json").ExtensionInstallForcelist;
assert builtins.elem extension (policies "chromium/policies/managed/default.json").ExtensionInstallForcelist;
assert !(builtins.any (package: lib.getName package == "chromium") enabled.environment.systemPackages);
assert builtins.all retriesOnlyDeferredWork maintenanceServices;
assert enabled.services.nixcare.deferUnderLoad;
assert enabled.systemd.services.nixcare-scan.serviceConfig.RestartForceExitStatus == [ 75 ];
assert !(enabled.systemd.services.nixcare-scan.unitConfig ? ConditionACPower);
assert !(enabled.systemd.services.nixcare-check-updates.serviceConfig ? RestartForceExitStatus);
assert lib.hasInfix "/etc/nixos/config's %%n/$$" enabled.systemd.services.nixcare-check-updates.serviceConfig.ExecStart;
assert !(fast.systemd.services.nixcare-scan.serviceConfig ? RestartForceExitStatus);
assert lib.hasSuffix "scan --fast" fast.systemd.services.nixcare-scan.serviceConfig.ExecStart;
assert !(unguarded.systemd.services.nixcare-scan.serviceConfig ? RestartForceExitStatus);
assert retriesOnlyDeferredWork unguarded.systemd.services.nixcare-update;
assert lib.hasInfix "/laptop_telemetry.py " (collectorExec enabled);
assert lib.hasInfix "--retention-days" (collectorExec enabled);
assert lib.hasInfix "/bin/nvme" (collectorExec enabled);
assert lib.hasSuffix "--activity" (collectorExec enabled);
assert !(lib.hasInfix "--activity" (collectorExec noActivity));
{
  browserExtensionPoliciesWorkWhenEnabled = true;
  browserExtensionPoliciesStayOptIn = true;
  chromiumIsNotInstalledByPolicy = true;
  deferredMaintenanceRetriesWithoutSkippingTheRun = true;
  vulnerabilityScansCanWaitForResources = true;
  ordinaryFailuresAndInputChecksDoNotRetry = true;
  inputCheckKeepsSpecialCharactersInPathsLiteral = true;
  fastScansAndResourceOptOutSkipLoadDeferrals = true;
  collectorGetsItsSettingsFromTheCommandLine = true;
}
