"""Synthetic checker inputs prove admission before network or installation effects."""

from tools.container_guard import require_container

require_container()

import copy
import fcntl
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import traceback
import unittest
import urllib.error
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tools import checker, checker_acquisition
from tools.wheelhouse import WheelhouseNetworkError
from tools.wheelhouse_acquire import pool_lock


class CheckerDependencyFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "dependencies.json"
        self.directory = self.root / "dependencies"
        self.directory.mkdir()
        self.shared = self.root / "shared"
        self.shared.mkdir()
        self.document = {
            "schema": "nmrnet.checker-dependencies.v1",
            "platform": "linux/amd64",
            "base": "docker.io/library/python:3.12.12-slim-bookworm@sha256:" + "a" * 64,
            "packages": [],
        }
        for name, version, filename in (
            ("git", "1:2.39.5-0+deb12u3", "git_2.39.5-0+deb12u3_amd64.deb"),
            ("make", "4.3-4.1", "make_4.3-4.1_amd64.deb"),
            ("ruff", "0.15.15", "ruff-0.15.15-py3-none-manylinux_2_17_x86_64.whl"),
        ):
            body = (name + " fixture bytes").encode()
            # silent-defaults: allow-contract -- Synthetic Ruff wheel declarations use the Python package host and Git/Make package declarations use the Debian host for exact checker admission; proof=tests/test_checker.py::CheckerDeclarationTests.test_valid_exact_inputs_and_receipt_are_accepted
            host = (
                "https://files.pythonhosted.org/packages/fixture/"
                if name == "ruff"
                else "https://deb.debian.org/debian/pool/main/fixture/"
            )
            self.document["packages"].append(
                {
                    "name": name,
                    "version": version,
                    "file": filename,
                    "url": host + filename,
                    "size": len(body),
                    "sha256": hashlib.sha256(body).hexdigest(),
                }
            )
            ((self.shared if name == "ruff" else self.directory) / filename).write_bytes(body)
        self.write()
        self.complete()

    def write(self):
        self.path.write_text(json.dumps(self.document))

    def complete(self):
        (self.directory / ".complete").write_text(json.dumps(checker.inventory(self.path, self.directory, self.shared)))


class CheckerDeclarationTests(CheckerDependencyFixture):
    def test_checker_entrypoint_needs_only_the_offline_image_files(self):
        tools_directory = self.root / "tools"
        tools_directory.mkdir()
        for name in ("checker.py", "artifacts.py", "container_guard.py"):
            shutil.copyfile(Path.cwd() / "tools" / name, tools_directory / name)
        environment = os.environ | {"PYTHONPATH": str(self.root)}
        result = subprocess.run(
            [sys.executable, "-B", "-m", "tools.checker", "--help"],
            cwd=self.root,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, msg=result.stderr)
        self.assertIn("install", result.stdout)

    def test_offline_install_does_not_import_wheelhouse(self):
        argv = [
            "checker",
            "install",
            "--declaration",
            str(self.path),
            "--directory",
            str(self.directory),
            "--base",
            self.document["base"],
            "--wheelhouse",
            str(self.root),
        ]
        with (
            patch.object(sys, "argv", argv),
            patch.dict(sys.modules, {"tools.wheelhouse": None, "tools.checker_acquisition": None}),
            patch.object(checker, "install") as install,
        ):
            checker.main()
        install.assert_called_once()

    def test_stream_check_dispatches_to_acquisition_with_the_original_arguments(self):
        pool = self.root / "pool"
        pool.mkdir()
        wheel = next(package["file"] for package in self.document["packages"] if package["name"] == "ruff")
        shutil.copyfile(self.shared / wheel, pool / wheel)
        stream = pool / "wheelhouse-tool-fixture.pyz"
        stream.write_bytes(b"verified stream fixture")
        pins = self.root / "stream.in"
        pins.write_text(
            "https://github.com/numpde/nmr-provider-tools/releases/download/v1/"
            + stream.name
            + "#sha256="
            + hashlib.sha256(stream.read_bytes()).hexdigest()
            + "\n"
        )
        expected = checker.check(self.path, self.directory, self.document["base"], pool)
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                "tools.checker",
                "check",
                "--declaration",
                str(self.path),
                "--directory",
                str(self.directory),
                "--base",
                self.document["base"],
                "--wheelhouse",
                str(pool),
                "--stream-assets",
                str(pins),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, msg=result.stderr)
        self.assertEqual(expected["declaration_sha256"] + "\n", result.stdout)
        self.assertEqual("", result.stderr)

    def test_checker_download_reports_bounded_network_error(self):
        argv = [
            "checker",
            "download",
            "--declaration",
            str(self.path),
            "--directory",
            str(self.directory),
            "--base",
            self.document["base"],
            "--wheelhouse",
            str(self.root),
            "--tool-assets",
            str(self.root / "tool"),
            "--stream-assets",
            str(self.root / "stream"),
        ]
        output = io.StringIO()
        with (
            patch.object(sys, "argv", argv),
            patch.object(
                checker_acquisition,
                "download",
                side_effect=WheelhouseNetworkError("Cannot acquire tool: the server returned HTTP 503."),
            ),
            redirect_stderr(output),
            self.assertRaises(SystemExit) as stopped,
        ):
            checker_acquisition.main()
        self.assertEqual(1, stopped.exception.code)
        self.assertIn("HTTP 503", output.getvalue())
        self.assertNotIn("Traceback", output.getvalue())

    def test_checker_stops_before_debian_download_when_ruff_publisher_fails(self):
        pool = self.root / "pool"
        pool.mkdir()
        tool = pool / "wheelhouse-tool-1.0.0.pyz"
        tool.write_bytes(b"tool bytes")
        pins = self.root / "pins.in"
        pins.write_text(
            "https://github.com/numpde/nmr-provider-tools/releases/download/v1/"
            + tool.name
            + "#sha256="
            + hashlib.sha256(tool.read_bytes()).hexdigest()
            + "\n"
        )
        failure = subprocess.CalledProcessError(7, ["tool", "https://secret.invalid/private"])
        output = io.StringIO()
        with (
            patch.object(checker.subprocess, "run", side_effect=failure),
            patch.object(checker_acquisition, "open_url") as network,
            redirect_stderr(output),
            self.assertRaises(SystemExit) as stopped,
        ):
            checker_acquisition.download(self.path, self.root / "fresh", self.document["base"], pool, pins, pins)
        self.assertEqual(7, stopped.exception.code)
        self.assertIn("checker Ruff wheel", output.getvalue())
        self.assertNotIn("secret.invalid", output.getvalue())
        network.assert_not_called()

    def test_valid_exact_inputs_and_receipt_are_accepted(self):
        self.assertEqual(
            checker.check(self.path, self.directory, self.document["base"], self.shared),
            checker.inventory(self.path, self.directory, self.shared),
        )

    def test_shared_wheel_validates_existing_receipt_without_private_wheel(self):
        wheel = next(package for package in self.document["packages"] if package["name"] == "ruff")
        pool = self.root / "pool"
        pool.mkdir()
        (pool / wheel["file"]).write_bytes((self.shared / wheel["file"]).read_bytes())
        receipt = checker.check(self.path, self.directory, self.document["base"], pool)
        self.assertEqual(receipt["files"][wheel["file"]]["sha256"], wheel["sha256"])
        (pool / wheel["file"]).write_bytes(b"wrong")
        with self.assertRaisesRegex(ValueError, "artifact bytes differ"):
            checker.check(self.path, self.directory, self.document["base"], pool)

    def test_unexpected_private_files_remain_rejected_with_shared_wheel(self):
        wheel = next(package for package in self.document["packages"] if package["name"] == "ruff")
        pool = self.root / "pool"
        pool.mkdir()
        (pool / wheel["file"]).write_bytes((self.shared / wheel["file"]).read_bytes())
        (self.directory / "unexpected").write_text("unexpected")
        with self.assertRaisesRegex(ValueError, "inventory differs"):
            checker.check(self.path, self.directory, self.document["base"], pool)

    def test_stale_private_dependencies_stop_before_shared_acquisition(self):
        (self.directory / self.document["packages"][0]["file"]).write_bytes(b"changed")
        pool = self.root / "pool"
        pool.mkdir()
        with (
            patch.object(checker_acquisition, "acquire_ruff") as acquire,
            self.assertRaisesRegex(ValueError, "artifact bytes differ"),
        ):
            checker_acquisition.download(self.path, self.directory, self.document["base"], pool, None, None)
        acquire.assert_not_called()

    def test_private_wheel_is_preserved_and_refused_before_shared_acquisition(self):
        wheel = self.document["packages"][-1]["file"]
        private = self.directory / wheel
        private.write_bytes((self.shared / wheel).read_bytes())
        before = private.read_bytes()
        with (
            patch.object(checker_acquisition, "acquire_ruff") as acquire,
            self.assertRaisesRegex(ValueError, "inventory differs"),
        ):
            checker_acquisition.download(self.path, self.directory, self.document["base"], self.shared)
        acquire.assert_not_called()
        self.assertEqual(before, private.read_bytes())

    def test_existing_receipt_allows_reacquiring_missing_shared_wheel(self):
        wheel = self.document["packages"][-1]["file"]
        content = (self.shared / wheel).read_bytes()
        (self.shared / wheel).unlink()
        with patch.object(checker_acquisition, "acquire_ruff", side_effect=lambda *_: (self.shared / wheel).write_bytes(content)):
            checker_acquisition.download(self.path, self.directory, self.document["base"], self.shared)
        self.assertEqual(content, (self.shared / wheel).read_bytes())

    def test_warm_checker_assets_do_not_wait_for_another_shared_pool_reader(self):
        wheel = self.document["packages"][-1]
        pool = self.root / "pool"
        pool.mkdir()
        shutil.copyfile(self.shared / wheel["file"], pool / wheel["file"])
        tool = pool / "wheelhouse-tool-1.0.0.pyz"
        tool.write_bytes(b"cached tool")
        pins = self.root / "pins.in"
        pins.write_text(
            "https://github.com/numpde/nmr-provider-tools/releases/download/v1/"
            + tool.name
            + "#sha256="
            + hashlib.sha256(tool.read_bytes()).hexdigest()
            + "\n"
        )
        failures = []

        def run():
            try:
                checker_acquisition.acquire_ruff(self.document, pool, pins, pins)
            except Exception as error:
                failures.append(error)

        with pool_lock(pool, exclusive=False):
            worker = threading.Thread(target=run)
            worker.start()
            worker.join(timeout=2)
            completed = not worker.is_alive()
        worker.join(timeout=5)
        self.assertTrue(completed, msg="warm checker assets waited for an exclusive pool lease")
        self.assertFalse(worker.is_alive())
        self.assertEqual([], failures)

    def test_corrupt_acquirer_refuses_before_missing_stream_download(self):
        pool = self.root / "pool"
        pool.mkdir()
        (pool / "acquirer.pyz").write_bytes(b"corrupt")
        acquirer = self.root / "acquirer.in"
        stream = self.root / "stream.in"
        prefix = "https://github.com/numpde/nmr-provider-tools/releases/download/v1/"
        acquirer.write_text(prefix + "acquirer.pyz#sha256=" + "a" * 64 + "\n")
        stream.write_text(prefix + "stream.pyz#sha256=" + "b" * 64 + "\n")
        with (
            patch.object(checker_acquisition, "checker_proxy") as proxy,
            self.assertRaisesRegex(ValueError, "cached bytes differ"),
        ):
            checker_acquisition.acquire_ruff(self.document, pool, acquirer, stream)
        proxy.assert_not_called()

    def test_unknown_missing_and_wrong_container_fields_are_rejected(self):
        for field in self.document:
            for change in ("missing", "extra", "type"):
                with self.subTest(field=field, change=change):
                    document = copy.deepcopy(self.document)
                    if change == "missing":
                        del document[field]
                    elif change == "extra":
                        document["unreviewed"] = True
                    else:
                        document[field] = None
                    self.path.write_text(json.dumps(document))
                    with self.assertRaises(ValueError):
                        checker.declaration(self.path)

    def test_package_fields_and_types_never_raise_accidental_type_errors(self):
        for field in self.document["packages"][0]:
            for change in ("missing", "extra", "type"):
                with self.subTest(field=field, change=change):
                    document = copy.deepcopy(self.document)
                    package = document["packages"][0]
                    if change == "missing":
                        del package[field]
                    elif change == "extra":
                        package["resolver_options"] = []
                    else:
                        package[field] = None
                    self.path.write_text(json.dumps(document))
                    with self.assertRaises(ValueError):
                        checker.declaration(self.path)

    def test_duplicate_json_fields_and_packages_are_rejected(self):
        self.path.write_text('{"schema":1,"schema":2}')
        with self.assertRaisesRegex(ValueError, "duplicate JSON"):
            checker.declaration(self.path)
        self.document["packages"].append(copy.deepcopy(self.document["packages"][0]))
        self.write()
        with self.assertRaisesRegex(ValueError, "duplicate package"):
            checker.declaration(self.path)

    def test_wrong_hosts_paths_architectures_and_versions_are_rejected(self):
        for field, value in (
            ("url", "https://example.invalid/git.deb"),
            ("url", "http://deb.debian.org/git.deb"),
            ("file", "../../escape.deb"),
            ("file", "git_2.39.5-0+deb12u3_arm64.deb"),
            ("version", "1:2.39.4"),
            ("size", True),
            ("sha256", "a" * 63),
        ):
            with self.subTest(field=field, value=value):
                document = copy.deepcopy(self.document)
                document["packages"][0][field] = value
                self.path.write_text(json.dumps(document))
                with self.assertRaises(ValueError):
                    checker.declaration(self.path)

    def test_stale_completion_does_not_admit_changed_missing_or_extra_bytes(self):
        filename = self.document["packages"][0]["file"]
        path = self.directory / filename
        original = path.read_bytes()
        path.write_bytes(b"changed")
        with self.assertRaises(ValueError) as caught:
            checker.check(self.path, self.directory, self.document["base"], self.shared)
        self.assertIn("artifact bytes differ from declaration", str(caught.exception))
        self.assertIn(repr(filename), str(caught.exception))
        path.unlink()
        with self.assertRaisesRegex(ValueError, "inventory"):
            checker.check(self.path, self.directory, self.document["base"], self.shared)
        path.write_bytes(original)
        (self.directory / "extra.deb").write_bytes(b"extra")
        with self.assertRaisesRegex(ValueError, "inventory"):
            checker.check(self.path, self.directory, self.document["base"], self.shared)

    def test_symlinked_artifact_cannot_reuse_a_valid_receipt(self):
        path = self.directory / self.document["packages"][0]["file"]
        original = self.root / "original"
        path.rename(original)
        path.symlink_to(original)
        with self.assertRaises(OSError):
            checker.check(self.path, self.directory, self.document["base"], self.shared)

    def test_changed_declaration_invalidates_an_existing_receipt(self):
        self.document["base"] = self.document["base"].replace("a" * 64, "b" * 64)
        self.write()
        with self.assertRaisesRegex(ValueError, "completion receipt"):
            checker.check(self.path, self.directory, self.document["base"], self.shared)

    def test_declaration_replacement_cannot_rebind_admitted_artifact_bytes(self):
        admitted_bytes = self.path.read_bytes()
        original_identity = checker.file_identity
        replaced = False

        def replace_declaration_before_artifact_hash(path):
            nonlocal replaced
            if not replaced:
                replaced = True
                self.document["base"] = self.document["base"].replace("a" * 64, "b" * 64)
                self.write()
            return original_identity(path)

        with patch.object(checker, "file_identity", side_effect=replace_declaration_before_artifact_hash):
            receipt = checker.inventory(self.path, self.directory, self.shared)
        self.assertTrue(replaced)
        self.assertEqual(receipt["declaration_sha256"], hashlib.sha256(admitted_bytes).hexdigest())
        self.assertNotEqual(receipt["declaration_sha256"], original_identity(self.path)["sha256"])
        (self.directory / ".complete").write_text(json.dumps(receipt))
        with self.assertRaisesRegex(ValueError, "completion receipt"):
            checker.check(self.path, self.directory, self.document["base"], self.shared)


class CheckerAcquisitionTests(CheckerDependencyFixture):
    def setUp(self):
        super().setUp()
        acquisition = patch.object(checker_acquisition, "acquire_ruff")
        acquisition.start()
        self.addCleanup(acquisition.stop)
        self.proxy = dict.fromkeys(("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"), "http://127.0.0.1:4321")
        self.proxy.update(dict.fromkeys(("NO_PROXY", "no_proxy", "ALL_PROXY", "all_proxy"), ""))

    def acquire(self, *, body=None, opener=None):
        output = self.root / "acquired"

        def response(request, **kwargs):
            filename = request.full_url.rsplit("/", 1)[-1]
            return io.BytesIO((self.directory / filename).read_bytes() if body is None else body)

        with (
            patch.dict(os.environ, self.proxy, clear=True),
            patch.object(checker_acquisition, "open_url", side_effect=opener or response),
        ):
            checker_acquisition.download(self.path, output, self.document["base"], self.shared)
        return output

    def test_acquisition_publishes_only_exact_verified_artifacts(self):
        output = self.acquire()
        self.assertEqual(
            checker.check(self.path, output, self.document["base"], self.shared),
            checker.check(self.path, self.directory, self.document["base"], self.shared),
        )
        self.assertEqual([], list(self.root.glob(".dependencies.pending-*")))

    def test_debian_download_failure_is_bounded_and_retains_stage(self):
        failure = urllib.error.HTTPError("https://secret.invalid/private", 503, "private response", {}, None)
        with (
            patch.dict(os.environ, self.proxy, clear=True),
            patch.object(checker_acquisition, "open_url", side_effect=failure) as network,
            self.assertRaises(WheelhouseNetworkError) as stopped,
        ):
            checker_acquisition.download(self.path, self.root / "acquired", self.document["base"], self.shared)
        network.assert_called_once()
        rendered = "".join(traceback.format_exception(stopped.exception))
        self.assertIn("HTTP 503", rendered)
        self.assertNotIn("secret.invalid", rendered)
        self.assertNotIn("private", rendered)
        self.assertEqual(1, len(list(self.root.glob(".dependencies.pending-*"))))
        self.assertFalse((self.root / "acquired").exists())

    def test_debian_read_timeout_retains_partial_without_publishing(self):
        response = unittest.mock.MagicMock()
        response.__enter__.return_value = response
        response.read.side_effect = [b"git", TimeoutError("private endpoint")]
        with (
            patch.dict(os.environ, self.proxy, clear=True),
            patch.object(checker_acquisition, "open_url", return_value=response) as network,
            self.assertRaises(WheelhouseNetworkError) as stopped,
        ):
            checker_acquisition.download(self.path, self.root / "acquired", self.document["base"], self.shared)
        network.assert_called_once()
        self.assertIn("connection timed out", str(stopped.exception))
        self.assertNotIn("private", "".join(traceback.format_exception(stopped.exception)))
        stages = list(self.root.glob(".dependencies.pending-*"))
        self.assertEqual(1, len(stages))
        self.assertEqual(b"git", (stages[0] / self.document["packages"][0]["file"]).read_bytes())
        self.assertFalse((self.root / "acquired").exists())

    def test_resume_reuses_verified_files_and_fetches_only_missing_debian_inputs(self):
        stage = self.root / ".dependencies.pending-example"
        stage.mkdir(mode=0o700)
        first, second, wheel = self.document["packages"]
        shutil.copyfile(self.directory / first["file"], stage / first["file"])
        pool = self.root / "pool"
        pool.mkdir()
        shutil.copyfile(self.shared / wheel["file"], pool / wheel["file"])
        requested = []

        def response(request, **kwargs):
            name = request.full_url.rsplit("/", 1)[-1]
            requested.append(name)
            return io.BytesIO((self.directory / name).read_bytes())

        with (
            patch.dict(os.environ, self.proxy, clear=True),
            patch.object(checker_acquisition, "acquire_ruff"),
            patch.object(checker_acquisition, "open_url", side_effect=response),
        ):
            checker_acquisition.download(self.path, self.root / "acquired", self.document["base"], pool, None, None, stage)
        self.assertEqual([second["file"]], requested)
        checker.check(self.path, self.root / "acquired", self.document["base"], pool)

    def test_resume_refuses_changed_evidence_before_any_acquisition(self):
        stage = self.root / ".dependencies.pending-example"
        stage.mkdir(mode=0o700)
        (stage / self.document["packages"][0]["file"]).write_bytes(b"changed")
        with (
            patch.object(checker_acquisition, "acquire_ruff") as acquire,
            patch.object(checker_acquisition, "open_url") as network,
            self.assertRaisesRegex(ValueError, "existing evidence preserved"),
        ):
            checker_acquisition.download(self.path, self.root / "acquired", self.document["base"], self.root, None, None, stage)
        acquire.assert_not_called()
        network.assert_not_called()
        self.assertTrue(stage.is_dir())

    def test_resume_publishes_complete_valid_stage_without_network(self):
        stage = self.root / ".dependencies.pending-example"
        stage.mkdir(mode=0o700)
        for package in self.document["packages"]:
            if package["name"] != "ruff":
                shutil.copyfile(self.directory / package["file"], stage / package["file"])
        shutil.copyfile(self.directory / ".complete", stage / ".complete")
        pool = self.root / "pool"
        pool.mkdir()
        wheel = self.document["packages"][-1]["file"]
        shutil.copyfile(self.shared / wheel, pool / wheel)
        with (
            patch.dict(os.environ, self.proxy, clear=True),
            patch.object(checker_acquisition, "acquire_ruff"),
            patch.object(checker_acquisition, "open_url") as network,
        ):
            checker_acquisition.download(self.path, self.root / "acquired", self.document["base"], pool, None, None, stage)
        network.assert_not_called()
        checker.check(self.path, self.root / "acquired", self.document["base"], pool)

    def test_new_stage_is_locked_while_downloading(self):
        observed = []

        def response(request, **kwargs):
            stage = next(self.root.glob(".dependencies.pending-*"))
            descriptor = os.open(stage, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                observed.append(request.full_url)
            finally:
                os.close(descriptor)
            name = request.full_url.rsplit("/", 1)[-1]
            return io.BytesIO((self.directory / name).read_bytes())

        self.acquire(opener=response)
        self.assertEqual(len(self.document["packages"]) - 1, len(observed))

    def test_invalid_declaration_and_proxy_fail_before_staging_or_network(self):
        with patch.object(checker_acquisition, "open_url") as network:
            self.document["unreviewed"] = True
            self.write()
            with self.assertRaises(ValueError):
                checker_acquisition.download(self.path, self.root / "acquired", self.document["base"], self.shared)
            network.assert_not_called()
        del self.document["unreviewed"]
        self.write()
        for name, value in (
            ("HTTPS_PROXY", "http://example.invalid:4321"),
            ("HTTP_PROXY", ""),
            ("NO_PROXY", "*"),
            ("ALL_PROXY", "http://127.0.0.1:4321"),
        ):
            with (
                self.subTest(name=name),
                patch.dict(os.environ, {**self.proxy, name: value}),
                patch.object(checker_acquisition, "open_url") as network,
            ):
                with self.assertRaisesRegex(ValueError, "launcher-owned"):
                    checker_acquisition.download(self.path, self.root / "acquired", self.document["base"], self.shared)
                network.assert_not_called()
        self.assertEqual([], list(self.root.glob(".dependencies.pending-*")))

    def test_missing_empty_malformed_and_mismatched_proxy_fields_reject_before_effects(self):
        cases = [
            ("HTTPS_PROXY", value)
            for value in (
                None,
                "",
                "http://example.invalid:4321",
                "https://127.0.0.1:4321",
                "http://127.0.0.1:0",
                "http://127.0.0.1:65536",
                "http://127.0.0.1:invalid",
            )
        ]
        cases.extend(
            (name, value) for name in ("HTTP_PROXY", "http_proxy", "https_proxy") for value in (None, "", "http://127.0.0.1:4322")
        )
        for name, value in cases:
            environment = dict(self.proxy)
            if value is None:
                del environment[name]
            else:
                environment[name] = value
            with (
                self.subTest(name=name, value=value),
                patch.dict(os.environ, environment, clear=True),
                patch.object(checker.tempfile, "mkdtemp") as stage,
                patch.object(checker_acquisition, "open_url") as network,
                self.assertRaisesRegex(ValueError, "launcher-owned"),
            ):
                checker_acquisition.download(self.path, self.root / "acquired", self.document["base"], self.shared)
            stage.assert_not_called()
            network.assert_not_called()

    def test_bypass_fields_admit_absence_and_empty_but_block_nonempty_before_effects(self):
        for name in ("NO_PROXY", "no_proxy", "ALL_PROXY", "all_proxy"):
            for value in (None, "", "*"):
                environment = dict(self.proxy)
                if value is None:
                    del environment[name]
                else:
                    environment[name] = value
                with (
                    self.subTest(name=name, value=value),
                    patch.dict(os.environ, environment, clear=True),
                    patch.object(checker.tempfile, "mkdtemp", side_effect=RuntimeError("stage reached")) as stage,
                    patch.object(checker_acquisition, "open_url") as network,
                ):
                    if value in (None, ""):
                        with self.assertRaisesRegex(RuntimeError, "stage reached"):
                            checker_acquisition.download(self.path, self.root / "acquired", self.document["base"], self.shared)
                        stage.assert_called_once()
                    else:
                        with self.assertRaisesRegex(ValueError, "launcher-owned"):
                            checker_acquisition.download(self.path, self.root / "acquired", self.document["base"], self.shared)
                        stage.assert_not_called()
                network.assert_not_called()

    def test_progress_bytes_are_flushed_before_first_transfer(self):
        output = io.StringIO()

        def response(request, **_kwargs):
            staged = next(self.root.glob(".dependencies.pending-*"))
            self.assertEqual(
                f"Acquiring checker dependencies into {staged}; incomplete staging is retained on failure.\n",
                output.getvalue(),
            )
            flush.assert_called_once()
            return io.BytesIO((self.directory / request.full_url.rsplit("/", 1)[-1]).read_bytes())

        with patch.object(sys, "stdout", output), patch.object(output, "flush", wraps=output.flush) as flush:
            self.acquire(opener=response)

    def test_failed_download_preserves_stage_without_complete_publication(self):
        for body in (b"short", b"x" * 100):
            with self.subTest(body_size=len(body)), self.assertRaises(ValueError):
                self.acquire(body=body)
        self.assertFalse((self.root / "acquired").exists())
        stages = list(self.root.glob(".dependencies.pending-*"))
        self.assertEqual(2, len(stages))
        self.assertTrue(all(not (stage / ".complete").exists() for stage in stages))

    def test_existing_verified_output_is_reused_without_network(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(checker.tempfile, "mkdtemp") as stage,
            patch.object(checker_acquisition, "open_url") as network,
        ):
            checker_acquisition.download(self.path, self.directory, self.document["base"], self.shared)
        stage.assert_not_called()
        network.assert_not_called()

    def test_existing_output_cannot_switch_declaration_and_receipt_after_admission(self):
        base = self.document["base"]
        admitted = checker.admitted_declaration
        original_receipt = checker.read_document(self.directory / ".complete")

        def replace_authority(path):
            result = admitted(path)
            self.document["base"] = self.document["base"].replace("a" * 64, "b" * 64)
            self.write()
            replacement_receipt = {**original_receipt, "declaration_sha256": hashlib.sha256(self.path.read_bytes()).hexdigest()}
            (self.directory / ".complete").write_text(json.dumps(replacement_receipt))
            return result

        with (
            patch.object(checker_acquisition, "admitted_declaration", side_effect=replace_authority),
            patch.object(checker_acquisition, "open_url") as network,
            self.assertRaisesRegex(ValueError, "completion receipt"),
        ):
            checker_acquisition.download(self.path, self.directory, base, self.shared)
        network.assert_not_called()

    def test_concurrent_output_is_not_replaced_at_publication(self):
        output = self.root / "acquired"
        publish = checker_acquisition.publish_directory

        def concurrent_publication(staged, destination):
            output.mkdir()
            (output / "predecessor").write_bytes(b"owned predecessor")
            publish(staged, destination)

        with (
            patch.object(checker_acquisition, "publish_directory", side_effect=concurrent_publication),
            self.assertRaises(FileExistsError),
        ):
            self.acquire()
        self.assertEqual(b"owned predecessor", (output / "predecessor").read_bytes())

    def test_check_cli_preserves_declaration_hash_and_newline(self):
        output = io.StringIO()
        argv = [
            "checker",
            "check",
            "--declaration",
            str(self.path),
            "--directory",
            str(self.directory),
            "--base",
            self.document["base"],
            "--wheelhouse",
            str(self.shared),
        ]
        with patch.object(sys, "argv", argv), patch.object(sys, "stdout", output):
            checker.main()
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest() + "\n", output.getvalue())

    def test_offline_install_refuses_changed_artifacts_before_any_installation(self):
        (self.directory / self.document["packages"][0]["file"]).write_bytes(b"changed")
        with patch.object(checker.subprocess, "run") as command, self.assertRaises(ValueError):
            checker.install(self.path, self.directory, self.document["base"], self.shared)
        command.assert_not_called()

    def test_offline_install_uses_only_declared_artifacts_and_checks_versions(self):
        calls = []

        def command(arguments, **kwargs):
            calls.append(arguments)
            if arguments[0] == "dpkg-query":
                package = next(package for package in self.document["packages"] if package["name"] == arguments[-1])
                return SimpleNamespace(stdout=package["version"])
            if arguments[0] == "ruff":
                return SimpleNamespace(stdout="ruff 0.15.15\n")
            if arguments[:4] == ["python3", "-m", "pip", "install"]:
                self.assertTrue({"--no-index", "--no-deps", "--require-hashes", "--only-binary=:all:"} <= set(arguments))
                lock = Path(arguments[arguments.index("--requirement") + 1]).read_text()
                self.assertIn("ruff==0.15.15 --hash=sha256:", lock)
            return SimpleNamespace(stdout="")

        with patch.object(checker.subprocess, "run", side_effect=command):
            checker.install(self.path, self.directory, self.document["base"], self.shared)
        expected = [str(self.directory / package["file"]) for package in self.document["packages"] if package["name"] != "ruff"]
        self.assertEqual(calls[0], ["dpkg", "--install", *expected])

    def test_base_mismatch_blocks_acquisition_verification_and_installation(self):
        with patch.object(checker_acquisition, "open_url") as network, patch.object(checker.subprocess, "run") as command:
            for action in (checker_acquisition.download, checker.check, checker.install):
                with self.subTest(action=action.__name__), self.assertRaisesRegex(ValueError, "another base"):
                    action(self.path, self.directory, "wrong base", self.shared)
        network.assert_not_called()
        command.assert_not_called()
