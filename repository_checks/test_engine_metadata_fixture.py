"""Keep engine metadata projection finite for current launcher integration.

tools/baseline.sh admits image ID and upstream revision in one observation;
tools/inference_image.sh retains its ID projection. These are contained engine
composition proofs; native Git and real engine identity proofs remain separate.
"""

from tools.container_guard import require_container

require_container()

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from repository_checks.fixtures.containers.fixture import LauncherFixture


class EngineMetadataFixtureTests(unittest.TestCase):
    def inspect(self, fixture, arguments, additions):
        return subprocess.run(
            [str(fixture.root / "bin/docker"), *arguments],
            env=fixture.environment | additions,
            capture_output=True,
            check=False,
        )

    def test_owned_empty_log_records_first_supported_projection(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LauncherFixture(Path(directory))
            self.assertEqual([], list(fixture.log.iterdir()))
            self.assertEqual([], fixture.calls())
            identity = "sha256:" + "b" * 64
            arguments = ["image", "inspect", "--format", "{{.Id}}", identity]
            result = self.inspect(fixture, arguments, {})
            self.assertEqual(0, result.returncode, msg=result.stderr)
            self.assertEqual((identity + "\n").encode(), result.stdout)
            self.assertEqual(b"", result.stderr)
            self.assertEqual([arguments], [call["arguments"] for call in fixture.calls()])

    def test_missing_call_log_cannot_certify_zero_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LauncherFixture(Path(directory))
            result = self.inspect(fixture, ["image", "inspect", "sha256:" + "b" * 64], {})
            self.assertEqual(0, result.returncode, msg=result.stderr)
            (fixture.log / "000000.json").unlink()
            fixture.log.rmdir()
            with self.assertRaises(FileNotFoundError):
                fixture.calls()

    def test_malformed_call_log_cannot_certify_zero_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LauncherFixture(Path(directory))
            result = self.inspect(fixture, ["image", "inspect", "sha256:" + "b" * 64], {})
            self.assertEqual(0, result.returncode, msg=result.stderr)
            (fixture.log / "000000.json").write_text("{invalid JSON\n")
            with self.assertRaises(json.JSONDecodeError):
                fixture.calls()

    def test_preexisting_call_log_is_preserved_and_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "engine.jsonl"
            evidence = b'{"arguments":["prior"]}\n'
            log.write_bytes(evidence)
            with self.assertRaises(FileExistsError):
                LauncherFixture(root)
            self.assertEqual(evidence, log.read_bytes())

    def test_incomplete_or_gapped_event_history_cannot_certify_calls(self):
        for name in ("000000.pending", "000001.json"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                fixture = LauncherFixture(Path(directory))
                arguments = ["image", "inspect", "sha256:" + "b" * 64]
                result = self.inspect(fixture, arguments, {})
                self.assertEqual(0, result.returncode, msg=result.stderr)
                (fixture.log / "000000.json").rename(fixture.log / name)
                with self.assertRaises(ValueError):
                    fixture.calls()

    def test_two_live_publishers_keep_all_ordered_events_under_the_inherited_cap(self):
        program = (
            "import resource, sys, time\n"
            "from pathlib import Path\n"
            "from repository_checks.fixtures.containers.fixture import record_engine_call\n"
            "resource.setrlimit(resource.RLIMIT_FSIZE, (4096, 4096))\n"
            "root, name = Path(sys.argv[1]), sys.argv[2]\n"
            "(root / (name + '.ready')).write_text('ready\\n')\n"
            "while not (root / 'publish').exists(): time.sleep(0.01)\n"
            "for ordinal in range(8):\n"
            "    record_engine_call(root / 'engine.jsonl', {'arguments': [name, str(ordinal), 'x' * 512]})\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            fixture = LauncherFixture(Path(directory))
            processes = []
            try:
                for name in ("left", "right"):
                    processes.append(
                        subprocess.Popen(
                            [sys.executable, "-B", "-c", program, directory, name],
                            env=fixture.environment,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                        )
                    )
                deadline = time.monotonic() + 3
                while not all((fixture.root / (name + ".ready")).exists() for name in ("left", "right")):
                    self.assertLess(time.monotonic(), deadline, msg="Both actual publishers did not reach admission")
                    time.sleep(0.01)
                self.assertTrue(all(process.poll() is None for process in processes))
                (fixture.root / "publish").write_text("go\n")
                for process in processes:
                    stdout, stderr = process.communicate(timeout=5)
                    self.assertEqual(0, process.returncode, msg=stderr)
                    self.assertEqual(b"", stdout)
                    self.assertEqual(b"", stderr)
                calls = fixture.calls()
                self.assertEqual(16, len(calls))
                for name in ("left", "right"):
                    self.assertEqual(
                        [str(index) for index in range(8)],
                        [call["arguments"][1] for call in calls if call["arguments"][0] == name],
                    )
                self.assertTrue(all(call["arguments"][2] == "x" * 512 for call in calls))
                sizes = [path.stat().st_size for path in fixture.log.iterdir()]
                self.assertGreater(sum(sizes), 4096)
                self.assertTrue(all(size < 4096 for size in sizes))
            finally:
                for process in processes:
                    if process.returncode is None:
                        process.kill()
                        process.communicate(timeout=3)

    def test_exact_metadata_projections_retain_literal_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LauncherFixture(Path(directory))
            identity = "sha256:" + "c" * 64
            revision = "d" * 40
            label = '{{ index .Config.Labels "io.nmrnet-repro.upstream-revision" }}'
            forms = (
                (["image", "inspect", identity], identity),
                (["image", "inspect", "--format", "{{.Id}}", identity], identity),
                (["image", "inspect", "--format", label, identity], revision),
                (["image", "inspect", "--format", "{{.Id}}|" + label, identity], identity + "|" + revision),
            )
            for arguments, expected in forms:
                with self.subTest(arguments=arguments):
                    result = self.inspect(
                        fixture,
                        arguments,
                        {
                            "NMRNET_FAKE_IMAGE_ID": identity,
                            "NMRNET_FAKE_IMAGE_REVISION": revision,
                        },
                    )
                    self.assertEqual(0, result.returncode, msg=result.stderr)
                    self.assertEqual((expected + "\n").encode(), result.stdout)
                    self.assertEqual(b"", result.stderr)
            self.assertEqual(113, len(result.stdout))

    def test_unknown_formats_and_nonexact_argv_reject_before_fault_responses(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LauncherFixture(Path(directory))
            identity = "sha256:" + "b" * 64
            label = '{{ index .Config.Labels "io.nmrnet-repro.upstream-revision" }}'
            requests = (
                ["image", "inspect", "--format", "contains-upstream-revision", identity],
                ["image", "inspect", "--format", label + "suffix", identity],
                ["image", "inspect", "--format", "{{.Id}}|" + label + "\n", identity],
                ["image", "inspect", "--format", "{{.Id}}|" + label + "|{{.Name}}", identity],
                ["image", "inspect", "--format {{.Id}}", identity],
                ["image", "inspect", "--format", "{{.Id}}", ""],
                ["image", "inspect", "--format", "{{.Id}}", identity, "extra"],
                ["image", "inspect", identity, "extra"],
                ["image", "inspect", "--format", "{{.Id}}", "--unexpected"],
            )
            private = "private-unmodeled-inspection-canary"
            for arguments in requests:
                with self.subTest(arguments=arguments):
                    result = self.inspect(
                        fixture,
                        arguments,
                        {
                            "NMRNET_FAKE_INSPECT_STATUS": "7",
                            "NMRNET_FAKE_INSPECT_PRIVATE_ERROR": private,
                        },
                    )
                    self.assertEqual(97, result.returncode, msg=result.stderr)
                    self.assertEqual(b"", result.stdout)
                    self.assertNotIn(private.encode(), result.stderr)
                    self.assertIn(b"unexpected fake docker invocation", result.stderr)
            self.assertEqual(list(requests), [call["arguments"] for call in fixture.calls()])

    def test_supported_projection_preserves_original_failure_controls(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LauncherFixture(Path(directory))
            identity = "sha256:" + "b" * 64
            label = '{{ index .Config.Labels "io.nmrnet-repro.upstream-revision" }}'
            forms = (
                ["image", "inspect", identity],
                ["image", "inspect", "--format", "{{.Id}}", identity],
                ["image", "inspect", "--format", label, identity],
                ["image", "inspect", "--format", "{{.Id}}|" + label, identity],
            )
            private = "private-supported-inspection-canary"
            for arguments in forms:
                for status in (7, 124, 137, 130, 143):
                    with self.subTest(arguments=arguments, status=status):
                        result = self.inspect(
                            fixture,
                            arguments,
                            {
                                "NMRNET_FAKE_INSPECT_STATUS": str(status),
                                "NMRNET_FAKE_INSPECT_PRIVATE_ERROR": private,
                            },
                        )
                        self.assertEqual(status, result.returncode)
                        self.assertEqual(b"", result.stdout)
                        self.assertEqual((private + "\n").encode(), result.stderr)

    def test_owned_empty_registry_preserves_two_container_lifetimes_and_native_statuses(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = LauncherFixture(root)
            state = root / "containers.json"
            self.assertEqual(b"{}\n", state.read_bytes())
            ready = root / "proxy-ready"
            ready.mkdir()
            proxy, worker = "a" * 64, "b" * 64
            create_proxy = [
                "create",
                "--cidfile",
                str(root / "proxy.cid"),
                "--mount",
                f"type=bind,src={ready},dst=/ready",
                "fixture-image",
            ]
            result = self.inspect(fixture, create_proxy, {"NMRNET_FAKE_CID": proxy})
            self.assertEqual(0, result.returncode, msg=result.stderr)
            self.assertEqual((proxy + "\n").encode(), result.stdout)
            self.assertEqual(b"", result.stderr)
            self.assertEqual({proxy: "proxy"}, json.loads(state.read_text()))
            started = self.inspect(fixture, ["start", proxy], {})
            self.assertEqual(0, started.returncode, msg=started.stderr)
            self.assertEqual("12345\n", (ready / "port").read_text())
            create_worker = ["create", "--cidfile", str(root / "worker.cid"), "fixture-image"]
            result = self.inspect(fixture, create_worker, {"NMRNET_FAKE_CID": worker})
            self.assertEqual(0, result.returncode, msg=result.stderr)
            self.assertEqual((worker + "\n").encode(), result.stdout)
            self.assertEqual({proxy: "proxy", worker: "worker"}, json.loads(state.read_text()))
            started = self.inspect(fixture, ["start", "--attach", worker], {"NMRNET_FAKE_WORKER_STATUS": "23"})
            self.assertEqual(23, started.returncode, msg=started.stderr)
            previous, inode = state.read_bytes(), state.stat().st_ino
            failed = self.inspect(fixture, ["rm", "--force", worker], {"NMRNET_FAKE_REMOVE_STATUS": "37"})
            self.assertEqual(37, failed.returncode, msg=failed.stderr)
            self.assertEqual(previous, state.read_bytes())
            self.assertEqual(inode, state.stat().st_ino)
            unknown = self.inspect(fixture, ["rm", "--force", "e" * 64], {})
            self.assertEqual(97, unknown.returncode, msg=unknown.stderr)
            self.assertEqual(b"unexpected fake docker invocation: unknown container identity\n", unknown.stderr)
            self.assertEqual(previous, state.read_bytes())
            self.assertEqual(inode, state.stat().st_ino)
            removed = self.inspect(fixture, ["rm", "--force", worker], {})
            self.assertEqual(0, removed.returncode, msg=removed.stderr)
            self.assertEqual(b"", removed.stdout)
            self.assertEqual({proxy: "proxy"}, json.loads(state.read_text()))
            removed = self.inspect(fixture, ["rm", "--force", proxy], {})
            self.assertEqual(0, removed.returncode, msg=removed.stderr)
            self.assertEqual({}, json.loads(state.read_text()))

    def test_preexisting_registry_is_preserved_and_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "containers.json"
            evidence = b'{"prior-container":"worker"}\n'
            state.write_bytes(evidence)
            inode = state.stat().st_ino
            with self.assertRaises(FileExistsError) as raised:
                LauncherFixture(root)
            self.assertEqual(str(state), raised.exception.filename)
            self.assertEqual(evidence, state.read_bytes())
            self.assertEqual(inode, state.stat().st_ino)

    def test_lost_registry_blocks_second_create_before_cid_and_role_effects(self):
        for fault, cause in (("missing", b"FileNotFoundError"), ("malformed", b"JSONDecodeError")):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                fixture = LauncherFixture(root)
                ready_a, ready_b = root / "ready-a", root / "ready-b"
                ready_a.mkdir()
                ready_b.mkdir()
                first = [
                    "create",
                    "--cidfile",
                    str(root / "first.cid"),
                    "--mount",
                    f"type=bind,src={ready_a},dst=/ready",
                    "fixture-image",
                ]
                result = self.inspect(fixture, first, {"NMRNET_FAKE_CID": "a" * 64})
                self.assertEqual(0, result.returncode, msg=result.stderr)
                started = self.inspect(fixture, ["start", "a" * 64], {})
                self.assertEqual(0, started.returncode, msg=started.stderr)
                preserved = {
                    path: (path.read_bytes(), path.stat().st_ino)
                    for path in (root / "first.cid", root / "proxy-cid-path", root / "ready-path")
                }
                state = root / "containers.json"
                if fault == "missing":
                    state.unlink()
                else:
                    state.write_bytes(b"{invalid JSON\n")
                    state_inode = state.stat().st_ino
                second = [
                    "create",
                    "--cidfile",
                    str(root / "second.cid"),
                    "--mount",
                    f"type=bind,src={ready_b},dst=/ready",
                    "fixture-image",
                ]
                result = self.inspect(fixture, second, {"NMRNET_FAKE_CID": "b" * 64})
                self.assertNotEqual(0, result.returncode)
                self.assertEqual(b"", result.stdout)
                self.assertIn(cause, result.stderr)
                self.assertFalse((root / "second.cid").exists())
                for path, (body, inode) in preserved.items():
                    self.assertEqual(body, path.read_bytes())
                    self.assertEqual(inode, path.stat().st_ino)
                self.assertFalse((ready_b / "port").exists())
                if fault == "missing":
                    self.assertFalse(state.exists())
                else:
                    self.assertEqual(b"{invalid JSON\n", state.read_bytes())
                    self.assertEqual(state_inode, state.stat().st_ino)
                self.assertEqual([first, ["start", "a" * 64], second], [call["arguments"] for call in fixture.calls()])

    def test_lost_registry_removal_preserves_the_missing_or_malformed_cause(self):
        for fault, cause in (("missing", b"FileNotFoundError"), ("malformed", b"JSONDecodeError")):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                fixture = LauncherFixture(root)
                receipt = root / "worker.cid"
                created = self.inspect(
                    fixture, ["create", "--cidfile", str(receipt), "fixture-image"], {"NMRNET_FAKE_CID": "a" * 64}
                )
                self.assertEqual(0, created.returncode, msg=created.stderr)
                state = root / "containers.json"
                preserved = {path: (path.read_bytes(), path.stat().st_ino) for path in (receipt, root / "worker-cid-path")}
                if fault == "missing":
                    state.unlink()
                else:
                    state.write_bytes(b"{invalid JSON\n")
                    state_inode = state.stat().st_ino
                result = self.inspect(fixture, ["rm", "--force", "a" * 64], {})
                self.assertNotEqual(0, result.returncode)
                self.assertIn(cause, result.stderr)
                self.assertNotIn(b"unknown container identity", result.stderr)
                self.assertEqual(b"", result.stdout)
                for path, (body, inode) in preserved.items():
                    self.assertEqual(body, path.read_bytes())
                    self.assertEqual(inode, path.stat().st_ino)
                if fault == "missing":
                    self.assertFalse(state.exists())
                else:
                    self.assertEqual(b"{invalid JSON\n", state.read_bytes())
                    self.assertEqual(state_inode, state.stat().st_ino)

    def test_create_keeps_malformed_cid_and_nul_receipt_fault_controls(self):
        for identifier, nul, status, suffix, expected in (
            ("not-canonical", False, 37, b"\n", {}),
            ("a" * 64, True, 0, b"\0", {"a" * 64: "worker"}),
        ):
            with self.subTest(identifier=identifier, nul=nul), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                fixture = LauncherFixture(root)
                receipt = root / "worker.cid"
                additions = {"NMRNET_FAKE_CID": identifier, "NMRNET_FAKE_ENGINE_STATUS": str(status)}
                if nul:
                    additions["NMRNET_FAKE_NUL_RECEIPT_ROLE"] = "worker"
                result = self.inspect(fixture, ["create", "--cidfile", str(receipt), "fixture-image"], additions)
                self.assertEqual(status, result.returncode, msg=result.stderr)
                self.assertEqual((identifier + "\n").encode(), result.stdout)
                self.assertEqual(b"", result.stderr)
                self.assertEqual(identifier.encode() + suffix, receipt.read_bytes())
                self.assertEqual(str(receipt), (root / "worker-cid-path").read_text())
                self.assertEqual(expected, json.loads((root / "containers.json").read_text()))
