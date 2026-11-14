# Exercise the module's generated arguments and platform branches without
# evaluating Home Manager/nixpkgs or starting services. No store writes or builds.
let
  lib = {
    optionals = condition: values: if condition then values else [];
    concatMapStringsSep = separator: function: values:
      builtins.concatStringsSep separator (map function values);
    replaceStrings = builtins.replaceStrings;
    mkIf = condition: value: if condition then value else {};
    mkMerge = values: builtins.foldl' (result: value: result // value) {} values;
    hasPrefix = prefix: value: builtins.substring 0 (builtins.stringLength prefix) value == prefix;
  };
  package = "/nix/store/test-nixcare";
  flakePath = "/home/alice/work's/%h/\${HOME}/flake with spaces";
  stateHome = "/home/alice/state %n/$HOME";
  evaluate = linux: path: checkUpdates:
    (import ../modules/home-manager.nix {
      inherit lib;
      pkgs.stdenv.hostPlatform = {
        isLinux = linux;
        isDarwin = !linux;
      };
      config = {
        xdg.stateHome = stateHome;
        services.nixcare = {
          enable = true;
          inherit package checkUpdates;
          flakePath = path;
        };
      };
    }).config;
  linux = evaluate true flakePath true;
  darwin = evaluate false flakePath true;
  noFlakeLinux = evaluate true null true;
  disabledCheckDarwin = evaluate false flakePath false;
  expectedLinuxCheck = builtins.concatStringsSep " " [
    "\"${package}/bin/nixcare\"" "\"check-updates\"" "\"--flake\""
    ("\"/home/alice/work's/%%h/$$" + "{HOME}/flake with spaces\"")
    "\"--output\"" "\"/home/alice/state %%n/$$HOME/nixcare/check-latest.json\""
  ];
in
assert linux.home.packages == [ package ];
assert linux.systemd.user.services.nixcare-check.Service.ExecStart == expectedLinuxCheck;
assert linux.systemd.user.services.nixcare-check.Service.Type == "oneshot";
assert linux.systemd.user.timers.nixcare-check.Timer.Persistent;
assert noFlakeLinux.systemd.user.services.nixcare-check == {};
assert noFlakeLinux.systemd.user.timers.nixcare-check == {};
assert darwin.launchd.agents.nixcare-check.config.ProgramArguments == [
  "${package}/bin/nixcare" "check-updates" "--flake" flakePath
  "--output" "${stateHome}/nixcare/check-latest.json"
];
assert darwin.launchd.agents.nixcare-scan.config.RunAtLoad;
assert disabledCheckDarwin.launchd.agents.nixcare-check == {};
{
  linuxArgumentsKeepPathsLiteral = true;
  darwinArgumentsRemainUnescaped = true;
  checksRespectOptOutAndMissingFlake = true;
}
