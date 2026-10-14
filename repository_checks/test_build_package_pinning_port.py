"""Preserve dapp system-package exact-version and admitted-command forms.

https://github.com/numpde/dapp32/blob/778c16f8d8b77181d2ac4cd11a62a2034ded2518/tests/checks/test_package_pinning.py
Its regexes, options, token parsing and uppercase assignment exception are unchanged.
Local inventory includes Dockerfile.lock as well as Dockerfile, rejects an empty
inventory, and excludes Dockerfile.dockerignore policies.
"""
from __future__ import annotations

from tools.container_guard import require_container

require_container()

import re
import unittest

from repository_checks.quality.sources import ROOT

APT_INSTALL_RE = re.compile(r"^(?P<manager>apt-get|apt) install (?P<body>.*)$")
APK_ADD_RE = re.compile(r"^apk add (?P<body>.*)$")
PACKAGE_COMMAND_RE = re.compile(r"^(?P<command>(?:apt-get|apt)\b.*\binstall\b.*|apk\b.*\badd\b.*)$")
ALLOWED_INSTALL_OPTIONS = {
    "apt": {"-y", "--no-install-recommends"},
    "apt-get": {"-y", "--no-install-recommends"},
    "apk": {"--no-cache"},
}


class PackagePinningTest(unittest.TestCase):
    def test_dockerfile_package_installs_are_pinned(self) -> None:
        failures: list[str] = []

        paths = [path for path in (ROOT / "containers").rglob("Dockerfile*") if not path.name.endswith(".dockerignore")]
        self.assertTrue(paths, msg="System-package Dockerfile inventory is empty.")
        for path in paths:

            failures.extend(self.package_pin_failures(path.read_text(), str(path)))

        if failures:
            self.fail("\n".join(failures))

    def package_pin_failures(self, text: str, path_label: str) -> list[str]:
        failures: list[str] = []
        logical_commands = self.logical_commands(text)

        for command in logical_commands:
            parts = self.split_shell_commands(command)
            for part in parts:
                failures.extend(self.package_command_failures(part, path_label))

        return failures

    def package_command_failures(self, command: str, path_label: str) -> list[str]:
        apt_match = APT_INSTALL_RE.match(command)
        apk_match = APK_ADD_RE.match(command)

        if apt_match is None and apk_match is None:
            if PACKAGE_COMMAND_RE.match(command):
                return [f"{path_label}: unexpected package install form: {command}"]
            return []

        if apt_match is not None:
            manager = apt_match.group("manager")
            body = apt_match.group("body")
        else:
            manager = "apk"
            body = apk_match.group("body")
        tokens = body.split()

        if any(token.startswith("-") and token not in ALLOWED_INSTALL_OPTIONS[manager] for token in tokens):
            return [f"{path_label}: unexpected package install form: {command}"]

        failures: list[str] = []
        for package in self.package_specs(tokens, ALLOWED_INSTALL_OPTIONS[manager]):
            if "=" not in package:
                failures.append(f"{path_label}: {manager} package is not pinned: {package}")
        return failures

    def logical_commands(self, text: str) -> list[str]:
        commands: list[str] = []
        for logical_line in self.join_continuations(text):
            if not logical_line.startswith("RUN "):
                continue

            commands.append(logical_line.removeprefix("RUN ").strip())

        return commands

    def split_shell_commands(self, command: str) -> list[str]:
        return [
            part.strip()
            for part in re.split(r"\s*(?:&&|;)\s*", command)
            if part.strip()
        ]

    def join_continuations(self, text: str) -> list[str]:
        logical_lines: list[str] = []
        current = ""

        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue

            if stripped.endswith("\\"):
                current += stripped[:-1] + " "
                continue

            logical_lines.append(current + stripped)
            current = ""

        if current:
            logical_lines.append(current)

        return logical_lines

    def package_specs(self, tokens: list[str], allowed_options: set[str]) -> list[str]:
        packages: list[str] = []

        for token in tokens:
            if token in allowed_options:
                continue
            if "=" in token and token.split("=", 1)[0].isupper():
                continue
            packages.append(token)

        return packages
