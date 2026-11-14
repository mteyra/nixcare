{ lib, stdenvNoCC, python3, makeWrapper, nix, systemd ? null }:

stdenvNoCC.mkDerivation {
  pname = "nixcare";
  version = "0.2.0";
  src = ./cli;
  nativeBuildInputs = [ makeWrapper ];
  dontBuild = true;
  installPhase = ''
    runHook preInstall
    install -Dm644 nixcare.py "$out/libexec/nixcare.py"
    makeWrapper ${python3}/bin/python3 "$out/bin/nixcare" \
      --add-flags "$out/libexec/nixcare.py" \
      --prefix PATH : ${lib.makeBinPath ([ nix ] ++ lib.optionals stdenvNoCC.hostPlatform.isLinux [ systemd ])}
    runHook postInstall
  '';
  meta = {
    description = "Practical Nix health checks and safe update discovery for Linux and macOS";
    homepage = "https://github.com/mteyra/nixcare";
    license = lib.licenses.mit;
    mainProgram = "nixcare";
    platforms = lib.platforms.unix;
  };
}
