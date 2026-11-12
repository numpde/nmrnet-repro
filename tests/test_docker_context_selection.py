#!/usr/bin/env python3
"""The operation keeps one explicitly selected local Docker endpoint."""

from tools.container_guard import require_container

require_container()

import os
import pty
import re
import select
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


def fake_docker():
    arguments = sys.argv[1:]
    with Path(os.environ["FAKE_CONTEXT_CALLS"]).open("a") as stream:
        stream.write(" ".join(arguments) + "\n")
    if arguments[:2] == ["context", "ls"]:
        names = os.environ.get("FAKE_CONTEXT_NAMES", "default\nlocal-a\nlocal-b\nremote")
        sys.stdout.write(names)
        sys.stdout.write("\n")
        return 0
    if (
        arguments[:2] != ["context", "inspect"]
        or arguments[2:4] != ["--format", "{{.Endpoints.docker.Host}}"]
        or len(arguments) != 5
    ):
        return 9
    context = arguments[4]
    if context == "default":
        endpoint = os.environ.get("FAKE_DEFAULT_ENDPOINT", "unix:///run/user/1000/docker-default.sock")
    elif context == "local-a":
        endpoint = Path(os.environ["FAKE_CONTEXT_ENDPOINTS"]).read_text().splitlines()[0]
    elif context == "local-b":
        endpoint = Path(os.environ["FAKE_CONTEXT_ENDPOINTS"]).read_text().splitlines()[1]
    elif context == "fixture-local":
        endpoint = os.environ.get("FAKE_LOCAL_ENDPOINT", "unix:///run/user/1000/fixture.sock")
    elif context == "remote":
        endpoint = "ssh://example.invalid"
    else:
        return 1
    sys.stdout.write(endpoint)
    sys.stdout.write("\n")
    return 0


class DockerContextSelectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        binary = self.directory / "bin"
        binary.mkdir()
        (binary / "docker").symlink_to(Path(__file__).resolve())
        self.endpoints = self.directory / "endpoints"
        self.endpoints.write_text("unix:///run/user/1000/docker-a.sock\nunix:///run/user/1000/docker-b.sock\n")
        self.context_calls = self.directory / "context-calls"
        self.environment = os.environ | {
            "PATH": str(binary) + ":" + os.environ["PATH"],
            "PYTHONPATH": str(Path.cwd()) + os.pathsep + os.environ.get("PYTHONPATH", ""),
            "FAKE_CONTEXT_ENDPOINTS": str(self.endpoints),
            "FAKE_CONTEXT_CALLS": str(self.context_calls),
        }
        self.program = (
            "source tools/docker_client.sh; "
            'docker_client_prepare print || { printf "%s" "$docker_client_diagnostic" >&2; exit 2; }; '
            'printf "CONTEXT=%s ENDPOINT=%s\\n" "$NMRNET_DOCKER_CONTEXT_INPUT" "$NMRNET_DOCKER_ENDPOINT_INPUT"; '
            'printf "%s\\n" "${engine[@]}"'
        )

    def command(self, environment, program=None, *, input_text=None):
        return subprocess.run(
            ["bash", "-c", self.program if program is None else program],
            env=environment,
            input=input_text,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_named_local_context_selects_its_endpoint_and_remote_is_refused(self):
        selected = self.environment | {
            "NMRNET_DOCKER_CONTEXT_INPUT": "local-a",
            "NMRNET_DOCKER_CONTEXT_INPUT_ORIGIN": "command line",
        }
        result = self.command(selected)
        self.assertEqual(0, result.returncode, msg=result.stderr)
        self.assertIn("CONTEXT=local-a ENDPOINT=unix:///run/user/1000/docker-a.sock", result.stdout)
        self.assertIn("unix:///run/user/1000/docker-a.sock", result.stdout)

        selected["NMRNET_DOCKER_CONTEXT_INPUT"] = "remote"
        result = self.command(selected)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("local Unix-socket context", result.stderr)
        self.assertNotIn("ssh://example.invalid", result.stdout + result.stderr)

    def test_interactive_menu_lists_only_local_contexts_and_uses_the_choice(self):
        master, slave = pty.openpty()
        process = subprocess.Popen(
            ["bash", "-c", self.program],
            env=self.environment,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            text=False,
        )
        os.close(slave)
        output = bytearray()
        os.write(master, b"3\n")
        while process.poll() is None:
            ready, _, _ = select.select([master], [], [], 0.2)
            if ready:
                try:
                    output.extend(os.read(master, 4096))
                except OSError:
                    break
        while True:
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break
            if not chunk:
                break
            output.extend(chunk)
        os.close(master)
        self.assertEqual(0, process.wait(), msg=output.decode(errors="replace"))
        rendered = output.decode(errors="replace")
        self.assertIn("local-a", rendered)
        self.assertIn("local-b", rendered)
        self.assertNotIn("remote", rendered)
        self.assertIn("CONTEXT=local-b ENDPOINT=unix:///run/user/1000/docker-b.sock", rendered)

    def test_unattended_omission_fails_without_selecting_default(self):
        result = self.command(self.environment)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("pass DOCKER_CONTEXT=<local-context>", result.stderr)
        self.assertNotIn("default", result.stdout + result.stderr)
        self.assertFalse(self.context_calls.exists())

    def test_inherited_snapshot_refuses_control_characters_before_context_or_engine_effects(self):
        for control in ("\x1b", "\x7f"):
            context = "inherited" + control + "context"
            with self.subTest(control=control):
                environment = self.environment | {
                    "NMRNET_DOCKER_CONTEXT_INPUT": context,
                    "NMRNET_DOCKER_ENDPOINT_INPUT": "unix:///run/user/1000/docker-a.sock",
                }
                result = self.command(environment)
                self.assertEqual(2, result.returncode)
                self.assertIn("named local Unix-socket context", result.stderr)
                self.assertNotIn(context, result.stderr)
                self.assertEqual("", result.stdout)
                self.assertFalse(self.context_calls.exists())

    def test_nested_launcher_keeps_endpoint_snapshot_after_context_metadata_changes(self):
        environment = self.environment | {
            "NMRNET_DOCKER_CONTEXT_INPUT": "local-a",
            "NMRNET_DOCKER_CONTEXT_INPUT_ORIGIN": "command line",
        }
        script = """source tools/docker_client.sh
docker_client_prepare print || exit
printf '%s\\n' "$NMRNET_DOCKER_ENDPOINT_INPUT"
printf 'unix:///run/user/1000/replaced.sock\\nunix:///run/user/1000/docker-b.sock\\n' > "$FAKE_CONTEXT_ENDPOINTS"
bash -c 'source tools/docker_client.sh; docker_client_prepare print || exit; printf "%s\\n" "$NMRNET_DOCKER_ENDPOINT_INPUT"; printf "%s\\n" "${engine[@]}"'
"""
        result = self.command(environment, script)
        self.assertEqual(0, result.returncode, msg=result.stderr)
        self.assertEqual(3, result.stdout.count("unix:///run/user/1000/docker-a.sock"))
        self.assertNotIn("replaced.sock", result.stdout)

    def test_help_does_not_select_or_inspect_a_docker_context(self):
        result = subprocess.run(
            ["make", "-s", "help"],
            env=self.environment,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, msg=result.stderr)
        self.assertIn("Every Docker-using command needs DOCKER_CONTEXT=<name>", result.stdout)
        self.assertFalse(self.context_calls.exists())

    def test_runtime_docker_operations_have_one_context_owner(self):
        root = Path.cwd()
        files = [root / "Makefile", *sorted((root / "tools").glob("*.sh"))]
        bare_calls = []
        hard_coded_endpoints = []
        for path in files:
            for number, line in enumerate(path.read_text().splitlines(), start=1):
                if line.lstrip().startswith("#"):
                    continue
                if re.search(r"\bdocker\s+(?:--[A-Za-z]|[A-Za-z])", line) and path.name != "docker_client.sh":
                    bare_calls.append(f"{path.relative_to(root)}:{number}")
                if "unix://" in line and path.name != "docker_client.sh":
                    hard_coded_endpoints.append(f"{path.relative_to(root)}:{number}")
        self.assertEqual([], bare_calls, msg="Docker commands must use the shared operation endpoint.")
        self.assertEqual([], hard_coded_endpoints, msg="Only the context owner may validate Unix endpoint syntax.")

    def test_make_forwards_only_command_line_context(self):
        recipe = '\t@printf "%s|%s|%s|%s\\n" "$$NMRNET_DOCKER_CONTEXT_INPUT" "$$NMRNET_DOCKER_CONTEXT_INPUT_ORIGIN" "$$NMRNET_DOCKER_ENDPOINT_INPUT" "$${DOCKER_CONTEXT-unset}"\n'
        makefile = "print-context:\n" + recipe + "second-context:\n" + recipe
        with tempfile.NamedTemporaryFile("w", delete=False) as stream:
            stream.write(makefile)
            extra_makefile = stream.name
        self.addCleanup(Path(extra_makefile).unlink, missing_ok=True)
        with Path(extra_makefile).open("a") as stream:
            stream.write(
                "nested-context:\n"
                '\t@printf "unix:///run/user/1000/replaced.sock\\nunix:///run/user/1000/docker-b.sock\\n" > "$$FAKE_CONTEXT_ENDPOINTS"; '
                f"env -u MAKEFLAGS -u MFLAGS -u MAKEOVERRIDES make --no-print-directory -f Makefile -f {extra_makefile} "
                "DOCKER_CONTEXT=local-a second-context\n"
            )
        command_line = subprocess.run(
            ["make", "-s", "-f", "Makefile", "-f", extra_makefile, "DOCKER_CONTEXT=local-b", "print-context", "second-context"],
            env=self.environment
            | {
                "DOCKER_CONTEXT": "remote",
                "NMRNET_DOCKER_CONTEXT_INPUT": "remote",
                "NMRNET_DOCKER_CONTEXT_INPUT_ORIGIN": "command line",
                "NMRNET_DOCKER_ENDPOINT_INPUT": "unix:///ambient.sock",
            },
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, command_line.returncode, msg=command_line.stderr)
        self.assertEqual(
            "local-b|command line|unix:///run/user/1000/docker-b.sock|unset\n"
            "local-b|command line|unix:///run/user/1000/docker-b.sock|unset\n",
            command_line.stdout,
        )
        self.assertEqual(
            ["context inspect --format {{.Endpoints.docker.Host}} local-b"],
            self.context_calls.read_text().splitlines(),
        )
        with tempfile.TemporaryDirectory() as temporary:
            canary = Path(temporary) / "shell-executed"
            malicious = f"bad';touch$IFS{canary};#"
            rejected = subprocess.run(
                ["make", "-s", "-f", "Makefile", "-f", extra_makefile, f"DOCKER_CONTEXT={malicious}", "print-context"],
                env=self.environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(0, rejected.returncode)
            self.assertFalse(canary.exists(), msg="Make interpolated context input as shell syntax.")

        self.context_calls.unlink()
        self.endpoints.write_text("unix:///run/user/1000/docker-a.sock\nunix:///run/user/1000/docker-b.sock\n")
        child = subprocess.run(
            ["make", "-s", "-f", "Makefile", "-f", extra_makefile, "DOCKER_CONTEXT=local-a", "nested-context"],
            env=self.environment,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, child.returncode, msg=child.stderr)
        self.assertEqual("local-a|command line|unix:///run/user/1000/replaced.sock|unset\n", child.stdout)
        self.assertEqual(
            [
                "context inspect --format {{.Endpoints.docker.Host}} local-a",
                "context inspect --format {{.Endpoints.docker.Host}} local-a",
            ],
            self.context_calls.read_text().splitlines(),
        )
        explicit_default = subprocess.run(
            ["make", "-s", "-f", "Makefile", "-f", extra_makefile, "DOCKER_CONTEXT=default", "print-context"],
            env=self.environment,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, explicit_default.returncode, msg=explicit_default.stderr)
        self.assertEqual("default|command line|unix:///run/user/1000/docker-default.sock|unset\n", explicit_default.stdout)
        calls_before_ambient = self.context_calls.read_text()
        ambient = subprocess.run(
            ["make", "-s", "-f", "Makefile", "-f", extra_makefile, "print-context"],
            env=self.environment
            | {
                "DOCKER_CONTEXT": "remote",
            },
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(0, ambient.returncode)
        self.assertIn("pass DOCKER_CONTEXT=<local-context>", ambient.stderr)
        self.assertEqual(calls_before_ambient, self.context_calls.read_text())

        master, slave = pty.openpty()
        process = subprocess.Popen(
            ["make", "-s", "-f", "Makefile", "-f", extra_makefile, "print-context"],
            env=self.environment,
            stdin=slave,
            stdout=slave,
            stderr=slave,
        )
        os.close(slave)
        output = bytearray()
        os.write(master, b"3\n")
        while process.poll() is None:
            ready, _, _ = select.select([master], [], [], 0.2)
            if ready:
                try:
                    output.extend(os.read(master, 4096))
                except OSError:
                    break
        while True:
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break
            if not chunk:
                break
            output.extend(chunk)
        os.close(master)
        self.assertEqual(0, process.wait(), msg=output.decode(errors="replace"))
        self.assertIn("local-a", output.decode(errors="replace"))
        self.assertIn("local-b", output.decode(errors="replace"))
        self.assertRegex(
            output.decode(errors="replace"),
            r"local-b\|(?:file|undefined)\|unix:///run/user/1000/docker-b.sock\|unset",
        )


if Path(sys.argv[0]).name == "docker":
    raise SystemExit(fake_docker())
if __name__ == "__main__":
    unittest.main()
