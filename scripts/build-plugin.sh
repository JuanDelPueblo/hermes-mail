#!/usr/bin/env bash
set -euo pipefail

command -v git > /dev/null || { echo "build-plugin: git is required" >&2; exit 1; }

cd "$(dirname "$0")/.."
out="dist/hermes-mail-plugin"

rm -rf "$out"
mkdir -p "$out/hermes_mail" "$out/dashboard/dist"
cp plugin.yaml LICENSE __init__.py tools.py triage.py "$out/"
cp -r skills "$out/skills"
cp dashboard/manifest.json dashboard/plugin_api.py "$out/dashboard/"
cp dashboard/dist/index.js "$out/dashboard/dist/"
cp hermes_mail/__init__.py hermes_mail/client.py "$out/hermes_mail/"

for file in \
  plugin.yaml __init__.py tools.py triage.py \
  skills/mail/SKILL.md \
  hermes_mail/__init__.py hermes_mail/client.py \
  dashboard/manifest.json dashboard/plugin_api.py dashboard/dist/index.js
do
  test -f "$out/$file" || { echo "build-plugin: missing $file" >&2; exit 1; }
done

for path in \
  hermes_mail/auth.py hermes_mail/settings.py hermes_mail/server.py hermes_mail/cli.py \
  scripts tests pyproject.toml deploy nix
do
  test ! -e "$out/$path" || { echo "build-plugin: $path must not be in the plugin" >&2; exit 1; }
done

# Hermes installs plugins from git sources only. The build directory becomes a
# git repository, so `hermes plugins install file://…` works on it.
git -C "$out" init -q -b main
git -C "$out" add -A
git -C "$out" -c user.name="hermes-mail build" -c user.email="build@hermes-mail.invalid" -c commit.gpgsign=false commit -q -m "hermes-mail plugin"
test -d "$out/.git" || { echo "build-plugin: no git repository in $out" >&2; exit 1; }

echo "plugin in $out"
