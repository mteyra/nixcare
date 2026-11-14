# Compatibility entrypoint for users who have not enabled flakes.
{ pkgs ? import <nixpkgs> { } }:
pkgs.callPackage ./package.nix { }
