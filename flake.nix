{
  description = "nixcare — Declarative NixOS system health scanner, policy cleaner, safe update checker, and local hardware-potential + screen-time tracker";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs = { self, nixpkgs, ... }: {
    nixosModules = {
      default = import ./modules/default.nix;
      nixcare = import ./modules/default.nix;
    };
  };
}
