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
  pname = "uni-mail";
  inherit version;

  src = lib.fileset.toSource {
    root = ../.;
    fileset = ../uni_mail;
  };

  nativeBuildInputs = [ makeWrapper ];
  dontBuild = true;

  installPhase = ''
    runHook preInstall
    mkdir -p $out/lib/uni-mail
    cp -r uni_mail $out/lib/uni-mail/
    ${python314.interpreter} -m compileall -q $out/lib/uni-mail
    for entry in server:uni-maild cli:uni-mail; do
      makeWrapper ${python314.interpreter} $out/bin/''${entry#*:} \
        --add-flags "-m uni_mail.''${entry%%:*}" \
        --set PYTHONPATH $out/lib/uni-mail \
        --set PYTHONSAFEPATH 1
    done
    runHook postInstall
  '';

  meta = {
    description = "IMAP index service and command line client for the Hermes agent";
    mainProgram = "uni-mail";
    license = lib.licenses.mit;
    platforms = lib.platforms.linux;
  };
}
