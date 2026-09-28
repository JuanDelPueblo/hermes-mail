{
  lib,
  stdenvNoCC,
  makeWrapper,
  python314,
}:
let
  version = "0.1.0";
in
stdenvNoCC.mkDerivation {
  pname = "hermes-mail";
  inherit version;

  src = lib.fileset.toSource {
    root = ../.;
    fileset = ../hermes_mail;
  };

  nativeBuildInputs = [ makeWrapper ];
  dontBuild = true;

  installPhase = ''
    runHook preInstall
    mkdir -p $out/lib/hermes-mail
    cp -r hermes_mail $out/lib/hermes-mail/
    ${python314.interpreter} -m compileall -q $out/lib/hermes-mail
    for entry in server:hermes-maild cli:hermes-mail; do
      makeWrapper ${python314.interpreter} $out/bin/''${entry#*:} \
        --add-flags "-m hermes_mail.''${entry%%:*}" \
        --set PYTHONPATH $out/lib/hermes-mail \
        --set PYTHONSAFEPATH 1
    done
    runHook postInstall
  '';

  meta = {
    description = "IMAP index service and command line client for the Hermes agent";
    mainProgram = "hermes-mail";
    license = lib.licenses.mit;
    platforms = lib.platforms.linux;
  };
}
