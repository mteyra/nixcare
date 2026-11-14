{
  description = "nixcare — practical Nix health checks everywhere, scheduled maintenance on NixOS";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    # Nixpkgs 26.11 removed Intel macOS. Keep the last supported branch for it.
    nixpkgs-intel-darwin.url = "github:NixOS/nixpkgs/nixpkgs-26.05-darwin";
  };

  outputs = { self, nixpkgs, nixpkgs-intel-darwin, ... }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin" ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
      pkgsFor = system:
        (if system == "x86_64-darwin" then nixpkgs-intel-darwin else nixpkgs).legacyPackages.${system};
    in {
      packages = forAllSystems (system:
        let pkgs = pkgsFor system;
        in rec {
          nixcare = pkgs.callPackage ./package.nix { };
          default = nixcare;
        });
      apps = forAllSystems (system: {
        default = {
          type = "app";
          program = "${self.packages.${system}.nixcare}/bin/nixcare";
          meta.description = "Portable Nix health checks";
        };
        nixcare = self.apps.${system}.default;
      });
      checks = forAllSystems (system:
        let pkgs = pkgsFor system;
        in {
          package = self.packages.${system}.nixcare;
          unit-tests = pkgs.runCommand "nixcare-unit-tests" {
            nativeBuildInputs = [ pkgs.python3 ];
          } ''
            cp -r ${self} source
            chmod -R u+w source
            cd source
            python -m unittest discover -s test -p 'test_*.py' -v
            touch "$out"
          '';
        });
      nixosModules = {
        default = import ./modules/default.nix;
        nixcare = import ./modules/default.nix;
      };
      homeManagerModules = {
        default = import ./modules/home-manager.nix;
        nixcare = import ./modules/home-manager.nix;
      };
    };
}
