# Run with: nix eval --impure --file test/nixos-module.nix
let
  flake = builtins.getFlake (toString ../.);
  lib = flake.inputs.nixpkgs.lib;
  configuration = browserExtensions:
    (lib.nixosSystem {
      system = "x86_64-linux";
      modules = [
        flake.nixosModules.default
        {
          system.stateVersion = "24.11";
          services.nixcare = {
            enable = true;
            clean.enable = true;
            update.enable = true;
            track = {
              enable = true;
              activity.enable = true;
              browserExtensions.enable = browserExtensions;
            };
          };
        }
      ];
    }).config;
  enabled = configuration true;
  disabled = configuration false;
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
assert !(enabled.systemd.services.nixcare-scan.serviceConfig ? RestartForceExitStatus);
assert !(enabled.systemd.services.nixcare-check-updates.serviceConfig ? RestartForceExitStatus);
{
  browserExtensionPoliciesWorkWhenEnabled = true;
  browserExtensionPoliciesStayOptIn = true;
  chromiumIsNotInstalledByPolicy = true;
  deferredMaintenanceRetriesWithoutSkippingTheRun = true;
  ordinaryFailuresAndLightweightChecksDoNotRetry = true;
}
