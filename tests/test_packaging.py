"""Tests for what ships: no Hermes code, the deployment files and the entry points."""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))

from uni_mail import config  # noqa: E402
from uni_mail.dispatch import config as dispatch_config  # noqa: E402

PACKAGE = ROOT / "uni_mail"
DEPLOY = ROOT / "deploy"
FORBIDDEN = ("hermes_mail", "mail_triage", "triage_")
# The only places where a forbidden string may stay, and which strings. Both
# name the removed `triage_retention_days` setting: the config parser warns that
# it is ignored, and the migration removes it from a hermes-mail config.
ALLOWED = {
    "uni_mail/config.py": {"triage_"},
    "uni_mail/migrate.py": {"triage_"},
}


def sources() -> list[Path]:
    return sorted(path for path in PACKAGE.rglob("*.py"))


def module_names() -> list[str]:
    names = []
    for path in sources():
        parts = list(path.relative_to(ROOT).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        names.append(".".join(parts))
    return names


class NoHermesCodeTest(unittest.TestCase):
    def test_sources_exist(self):
        self.assertIn("uni_mail.server", module_names())
        self.assertIn("uni_mail.dispatch.runner", module_names())

    def test_no_import_of_hermes_or_triage_modules(self):
        for path in sources():
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    imported = [("." * node.level) + (node.module or "")] + [alias.name for alias in node.names]
                else:
                    continue
                for name in imported:
                    lowered = name.lower()
                    self.assertFalse(
                        any(part.startswith(("hermes", "nanobot")) or "triage" in part for part in lowered.strip(".").split(".")),
                        f"{path.relative_to(ROOT)} imports {name}",
                    )

    def test_no_hermes_or_triage_strings_outside_the_allowlist(self):
        for path in sources():
            relative = str(path.relative_to(ROOT))
            text = path.read_text()
            for forbidden in FORBIDDEN:
                if forbidden in text:
                    self.assertIn(forbidden, ALLOWED.get(relative, set()), f"{relative} contains {forbidden!r}")
        for relative, strings in ALLOWED.items():
            text = (ROOT / relative).read_text()
            for forbidden in strings:
                self.assertIn(forbidden, text, f"{relative} no longer needs its allowlist entry for {forbidden!r}")

    def test_importing_every_module_loads_no_hermes_or_nanobot_module(self):
        names = [name for name in module_names() if name != "uni_mail.mcp_server" or importlib.util.find_spec("mcp")]
        code = (
            "import importlib, json, sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "for name in json.loads(sys.argv[2]):\n"
            "    importlib.import_module(name)\n"
            "print(json.dumps(sorted(m for m in sys.modules if m.lower().startswith(('hermes', 'nanobot')))))\n"
        )
        result = subprocess.run([sys.executable, "-I", "-c", code, str(ROOT), json.dumps(names)],
                                capture_output=True, text=True, timeout=120, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout.splitlines()[-1]), [])


class DeploymentFilesTest(unittest.TestCase):
    FILES = (
        "uni-mail.service",
        "uni-mail-dispatch.service",
        "user/uni-mail.service",
        "user/uni-mail-dispatch.service",
        "sysusers.d/uni-mail.conf",
        "tmpfiles.d/uni-mail.conf",
        "config.example.json",
    )

    def test_files_exist(self):
        for name in self.FILES:
            self.assertTrue((DEPLOY / name).is_file(), f"deploy/{name} is missing")

    def test_hermes_is_named_only_in_a_conflicts_line(self):
        for path in sorted(DEPLOY.rglob("*")):
            if not path.is_file():
                continue
            for number, line in enumerate(path.read_text().splitlines(), start=1):
                if "hermes" in line.lower():
                    self.assertTrue(line.strip().startswith("Conflicts="),
                                    f"{path.relative_to(ROOT)}:{number} mentions hermes outside Conflicts=: {line.strip()}")

    def test_daemon_unit(self):
        unit = (DEPLOY / "uni-mail.service").read_text()
        starts = [line for line in unit.splitlines() if line.startswith("ExecStart=")]
        self.assertEqual(len(starts), 1, starts)
        self.assertIn("uni-maild", starts[0])
        self.assertIn("/etc/uni-mail/config.json", starts[0])

    def test_dispatch_unit(self):
        unit = (DEPLOY / "uni-mail-dispatch.service").read_text()
        starts = [line for line in unit.splitlines() if line.startswith("ExecStart=")]
        self.assertEqual(len(starts), 1, starts)
        self.assertIn("uni-mail", starts[0])
        self.assertIn("dispatch", starts[0])
        self.assertIn("/etc/uni-mail/config.json", starts[0])

    def test_user_units(self):
        for name, program in (("uni-mail.service", "uni-maild"), ("uni-mail-dispatch.service", "uni-mail")):
            unit = (DEPLOY / "user" / name).read_text()
            starts = [line for line in unit.splitlines() if line.startswith("ExecStart=")]
            self.assertEqual(starts, [f"ExecStart=%h/.local/share/uni-mail/venv/bin/{program} --config %E/uni-mail/config.json"
                                      + (" dispatch run" if program == "uni-mail" else "")])
            # A user manager cannot switch users, and the default target of a user is default.target.
            self.assertNotIn("User=", unit)
            self.assertIn("WantedBy=default.target", unit)
            self.assertIn("UMask=0077", unit)

    def test_example_config_parses(self):
        raw = json.loads((DEPLOY / "config.example.json").read_text())
        parsed = config.parse(raw)
        self.assertTrue(parsed.accounts)
        for name, account in raw["accounts"].items():
            self.assertNotIn("notify", account, name)
        self.assertNotIn("triage_retention_days", raw)
        self.assertIn("dispatch", raw)
        dispatch = dispatch_config.parse(raw["dispatch"])
        self.assertIsInstance(dispatch, dispatch_config.DispatchConfig)


class EntryPointsTest(unittest.TestCase):
    def setUp(self):
        self.project = tomllib.loads((ROOT / "pyproject.toml").read_text())

    def test_metadata(self):
        project = self.project["project"]
        self.assertEqual(project["name"], "uni-mail")
        self.assertEqual(project.get("dependencies", []), [])
        extras = project["optional-dependencies"]
        self.assertEqual(list(extras), ["mcp"])
        self.assertTrue(all(requirement.startswith("mcp") for requirement in extras["mcp"]))
        self.assertTrue(extras["mcp"])

    def test_scripts(self):
        self.assertEqual(self.project["project"]["scripts"], {
            "uni-mail": "uni_mail.cli:main",
            "uni-maild": "uni_mail.server:main",
            "uni-mail-mcp": "uni_mail.mcp_server:main",
        })

    def test_packages(self):
        packages = self.project["tool"]["setuptools"]["packages"]
        self.assertIn("uni_mail", packages)
        self.assertIn("uni_mail.dispatch", packages)
        on_disk = {name for name in module_names() if (ROOT / Path(*name.split("."))).is_dir()}
        self.assertLessEqual(on_disk, set(packages), "a package directory is not listed in pyproject.toml")


@unittest.skipUnless(os.environ.get("UNI_MAIL_TEST_INSTALL") == "1", "set UNI_MAIL_TEST_INSTALL=1 to install the package with uv")
class InstallTest(unittest.TestCase):
    def test_install_without_extras_and_run_the_commands(self):
        uv = shutil.which("uv")
        if uv is None:
            self.skipTest("uv is not installed")
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            # Build from a copy: setuptools would write build/ and *.egg-info into the repository.
            source = base / "source"
            source.mkdir()
            for name in ("pyproject.toml", "README.md", "LICENSE"):
                shutil.copy2(ROOT / name, source / name)
            shutil.copytree(PACKAGE, source / "uni_mail", ignore=shutil.ignore_patterns("__pycache__"))
            venv = base / "venv"
            self.run_checked([uv, "venv", "--python", "3.14", str(venv)])
            self.run_checked([uv, "pip", "install", "--python", str(venv / "bin" / "python"), str(source)])
            environment = {key: value for key, value in os.environ.items() if not key.startswith(("UNI_MAIL_", "PYTHON"))}
            for command in ("uni-mail", "uni-maild"):
                result = subprocess.run([str(venv / "bin" / command), "--help"], capture_output=True, text=True,
                                        timeout=60, check=False, env=environment, cwd=base)
                self.assertEqual(result.returncode, 0, f"{command}: {result.stderr}")
                self.assertIn(command, result.stdout)
            mcp = subprocess.run([str(venv / "bin" / "uni-mail-mcp")], capture_output=True, text=True,
                                 timeout=60, check=False, env=environment, cwd=base, stdin=subprocess.DEVNULL)
            self.assertEqual(mcp.returncode, 1, mcp.stdout + mcp.stderr)
            self.assertIn("needs the mcp extra", mcp.stdout + mcp.stderr)

    def run_checked(self, command: list[str]) -> None:
        result = subprocess.run(command, capture_output=True, text=True, timeout=600, check=False)
        self.assertEqual(result.returncode, 0, f"{' '.join(command)}\n{result.stdout}\n{result.stderr}")


if __name__ == "__main__":
    unittest.main()
