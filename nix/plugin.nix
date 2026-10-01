{
  lib,
  stdenvNoCC,
}:
# The Hermes plugin directory. It holds only the files that Hermes loads: the
# plugin, its skill, its dashboard page and the socket client. The service code, the tests and the
# probe are not part of it.
stdenvNoCC.mkDerivation {
  # services.hermes-agent.extraPlugins names the plugin link after this name.
  pname = "hermes-mail";
  version = "0.1.0";

  src = lib.fileset.toSource {
    root = ../.;
    fileset = lib.fileset.unions [
      ../plugin.yaml
      ../__init__.py
      ../tools.py
      ../triage.py
      ../skills
      ../dashboard
      ../hermes_mail/__init__.py
      ../hermes_mail/client.py
      ../LICENSE
    ];
  };

  dontBuild = true;

  installPhase = ''
    runHook preInstall
    cp -r . $out
    runHook postInstall
  '';

  meta = {
    description = "Hermes plugin for hermes-mail";
    license = lib.licenses.mit;
  };
}
