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
in
assert enabled.programs.chromium.enable;
assert !disabled.programs.chromium.enable;
assert !(disabled.environment.etc ? "opt/chrome/policies/managed/default.json");
assert builtins.elem extension (policies "opt/chrome/policies/managed/default.json").ExtensionInstallForcelist;
assert builtins.elem extension (policies "chromium/policies/managed/default.json").ExtensionInstallForcelist;
assert !(builtins.any (package: lib.getName package == "chromium") enabled.environment.systemPackages);
{
  browserExtensionPoliciesWorkWhenEnabled = true;
  browserExtensionPoliciesStayOptIn = true;
  chromiumIsNotInstalledByPolicy = true;
}
