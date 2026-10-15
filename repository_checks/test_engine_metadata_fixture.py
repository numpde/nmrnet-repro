"""Keep engine metadata projection finite for current launcher integration.

tools/baseline.sh admits image ID and upstream revision in one observation;
tools/inference_image.sh retains its ID projection. These are contained engine
composition proofs; native Git and real engine identity proofs remain separate.
"""

from tools.container_guard import require_container

require_container()

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

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

    def test_constructor_owns_metadata_and_explicit_fault_values_remain_literal(self):
        poisoned = {"NMRNET_FAKE_IMAGE_ID": "ambient-image-poison", "NMRNET_FAKE_IMAGE_REVISION": "ambient-revision-poison"}
        with patch.dict(os.environ, poisoned), tempfile.TemporaryDirectory() as directory:
            fixture = LauncherFixture(Path(directory))
            identity = "sha256:" + "b" * 64
            revision = "a" * 40
            self.assertEqual(identity, fixture.environment["NMRNET_FAKE_IMAGE_ID"])
            self.assertEqual(revision, fixture.environment["NMRNET_FAKE_IMAGE_REVISION"])
            label = '{{ index .Config.Labels "io.nmrnet-repro.upstream-revision" }}'
            arguments = ["image", "inspect", "--format", "{{.Id}}|" + label, identity]
            result = self.inspect(fixture, arguments, {})
            self.assertEqual(0, result.returncode, msg=result.stderr)
            self.assertEqual((identity + "|" + revision + "\n").encode(), result.stdout)
            self.assertEqual(b"", result.stderr)
            for image, source_revision in (("mutable:tag", "not-a-revision\n"), ("", "")):
                with self.subTest(image=image, revision=source_revision):
                    result = self.inspect(
                        fixture,
                        arguments,
                        {"NMRNET_FAKE_IMAGE_ID": image, "NMRNET_FAKE_IMAGE_REVISION": source_revision},
                    )
                    self.assertEqual(0, result.returncode, msg=result.stderr)
                    self.assertEqual((image + "|" + source_revision + "\n").encode(), result.stdout)
                    self.assertEqual(b"", result.stderr)

    def test_constructor_owns_removal_status_and_missing_cannot_remove_registered_identity(self):
        with (
            patch.dict(os.environ, {"NMRNET_FAKE_REMOVE_STATUS": "37"}),
            tempfile.TemporaryDirectory() as directory,
        ):
            fixture = LauncherFixture(Path(directory))
            self.assertEqual("0", fixture.environment["NMRNET_FAKE_REMOVE_STATUS"])
            workers = ("d" * 64, "e" * 64)
            for worker in workers:
                created = self.inspect(
                    fixture,
                    ["create", "--cidfile", str(fixture.root / (worker + ".cid")), "fixture-image"],
                    {"NMRNET_FAKE_CID": worker},
                )
                self.assertEqual(0, created.returncode, msg=created.stderr)
                self.assertEqual((worker + "\n").encode(), created.stdout)
            registry = fixture.root / "containers.json"
            ordinary = self.inspect(fixture, ["rm", "--force", workers[0]], {})
            self.assertEqual(0, ordinary.returncode, msg=ordinary.stderr)
            self.assertEqual(b"", ordinary.stdout)
            self.assertEqual(b"", ordinary.stderr)
            self.assertEqual({workers[1]: "worker"}, json.loads(registry.read_text()))
            before = (registry.read_bytes(), registry.stat().st_ino)
            failed = self.inspect(fixture, ["rm", "--force", workers[1]], {"NMRNET_FAKE_REMOVE_STATUS": "37"})
            self.assertEqual(37, failed.returncode)
            self.assertEqual(b"", failed.stdout)
            self.assertEqual(b"", failed.stderr)
            self.assertEqual(before, (registry.read_bytes(), registry.stat().st_ino))
            del fixture.environment["NMRNET_FAKE_REMOVE_STATUS"]
            missing = self.inspect(fixture, ["rm", "--force", workers[1]], {})
            self.assertEqual(1, missing.returncode)
            self.assertEqual(b"", missing.stdout)
            self.assertIn(b"KeyError: 'NMRNET_FAKE_REMOVE_STATUS'", missing.stderr)
            self.assertEqual(before, (registry.read_bytes(), registry.stat().st_ino))
            self.assertEqual(5, len(fixture.calls()))

    def test_constructor_owns_build_result_only_when_identity_output_consumes_it(self):
        with (
            patch.dict(os.environ, {"NMRNET_FAKE_BUILD_RESULT": "sha256:" + "e" * 64}),
            tempfile.TemporaryDirectory() as directory,
        ):
            fixture = LauncherFixture(Path(directory))
            identity = "sha256:" + "b" * 64
            self.assertEqual(identity, fixture.environment["NMRNET_FAKE_BUILD_RESULT"])
            arguments = [
                "build",
                "--file",
                str(fixture.root / "containers/inference/Dockerfile"),
                "--build-context",
                "wheelhouse=" + str(fixture.root / ".work/wheelhouse/cpu"),
                str(fixture.root),
            ]
            registry = fixture.root / "containers.json"
            before = (registry.read_bytes(), registry.stat().st_ino)
            cases = (
                ({}, identity, 0),
                ({"NMRNET_FAKE_BUILD_RESULT": ""}, "", 0),
                ({"NMRNET_FAKE_BUILD_RESULT": "mutable:tag\nsecond"}, "mutable:tag\nsecond", 0),
                ({"NMRNET_FAKE_ENGINE_STATUS": "37"}, identity, 37),
                ({"NMRNET_FAKE_ENGINE_STATUS": "37", "NMRNET_FAKE_BUILD_STATUS": "7"}, identity, 7),
                ({"NMRNET_FAKE_ENGINE_STATUS": "37", "NMRNET_FAKE_BUILD_STATUS": "0"}, identity, 0),
            )
            for additions, expected, status in cases:
                with self.subTest(additions=additions):
                    result = self.inspect(fixture, arguments, additions)
                    self.assertEqual(status, result.returncode, msg=result.stderr)
                    self.assertEqual((expected + "\n").encode(), result.stdout)
                    self.assertEqual(b"", result.stderr)
            del fixture.environment["NMRNET_FAKE_BUILD_RESULT"]
            evidence = fixture.root / "build-evidence.json"
            evidence.unlink()
            missing = self.inspect(fixture, arguments, {})
            self.assertEqual(1, missing.returncode)
            self.assertEqual(b"", missing.stdout)
            self.assertIn(b"KeyError: 'NMRNET_FAKE_BUILD_RESULT'", missing.stderr)
            # Evidence is written before identity output consumes its result.
            self.assertTrue(evidence.is_file())
            selected_bytes = self.inspect(fixture, arguments, {"NMRNET_FAKE_BUILD_BYTES": "3"})
            self.assertEqual(0, selected_bytes.returncode, msg=selected_bytes.stderr)
            self.assertEqual(b"xxx\n", selected_bytes.stdout)
            self.assertEqual(b"", selected_bytes.stderr)
            self.assertEqual([arguments] * (len(cases) + 2), [call["arguments"] for call in fixture.calls()])
            self.assertEqual(before, (registry.read_bytes(), registry.stat().st_ino))

    def test_constructor_owns_shared_engine_status_and_missing_cannot_synthesize_success(self):
        with (
            patch.dict(os.environ, {"NMRNET_FAKE_ENGINE_STATUS": "37"}),
            tempfile.TemporaryDirectory() as directory,
        ):
            fixture = LauncherFixture(Path(directory))
            self.assertEqual("0", fixture.environment["NMRNET_FAKE_ENGINE_STATUS"])
            identity = "sha256:" + "b" * 64
            arguments = ["image", "inspect", "--format", "{{.Id}}", identity]
            registry = fixture.root / "containers.json"
            before = (registry.read_bytes(), registry.stat().st_ino)
            for additions, expected in (({}, 0), ({"NMRNET_FAKE_ENGINE_STATUS": "37"}, 37)):
                with self.subTest(additions=additions):
                    result = self.inspect(fixture, arguments, additions)
                    self.assertEqual(expected, result.returncode, msg=result.stderr)
                    self.assertEqual((identity + "\n").encode(), result.stdout)
                    self.assertEqual(b"", result.stderr)
            del fixture.environment["NMRNET_FAKE_ENGINE_STATUS"]
            missing = self.inspect(fixture, arguments, {})
            self.assertEqual(1, missing.returncode)
            # Image output precedes terminal status consumption; preserve that effect.
            self.assertEqual((identity + "\n").encode(), missing.stdout)
            self.assertIn(b"KeyError: 'NMRNET_FAKE_ENGINE_STATUS'", missing.stderr)
            self.assertEqual([arguments] * 3, [call["arguments"] for call in fixture.calls()])
            self.assertEqual(before, (registry.read_bytes(), registry.stat().st_ino))

    def test_constructor_owns_worker_result_and_missing_status_cannot_synthesize_success(self):
        with (
            patch.dict(os.environ, {"NMRNET_FAKE_WORKER_STATUS": "37"}),
            tempfile.TemporaryDirectory() as directory,
        ):
            fixture = LauncherFixture(Path(directory))
            self.assertEqual("0", fixture.environment["NMRNET_FAKE_WORKER_STATUS"])
            worker = "d" * 64
            created = self.inspect(
                fixture,
                ["create", "--cidfile", str(fixture.root / "worker.cid"), "fixture-image"],
                {"NMRNET_FAKE_CID": worker, "NMRNET_FAKE_ENGINE_STATUS": "0"},
            )
            self.assertEqual(0, created.returncode, msg=created.stderr)
            self.assertEqual((worker + "\n").encode(), created.stdout)
            state = fixture.root / "containers.json"
            before = (state.read_bytes(), state.stat().st_ino)
            for additions, expected in (({}, 0), ({"NMRNET_FAKE_WORKER_STATUS": "37"}, 37)):
                with self.subTest(additions=additions):
                    result = self.inspect(fixture, ["start", "--attach", worker], additions)
                    self.assertEqual(expected, result.returncode, msg=result.stderr)
                    self.assertEqual(b"", result.stdout)
                    self.assertEqual(b"", result.stderr)
            del fixture.environment["NMRNET_FAKE_WORKER_STATUS"]
            missing = self.inspect(fixture, ["start", "--attach", worker], {})
            self.assertEqual(1, missing.returncode)
            self.assertEqual(b"", missing.stdout)
            self.assertIn(b"KeyError: 'NMRNET_FAKE_WORKER_STATUS'", missing.stderr)
            self.assertEqual(before, (state.read_bytes(), state.stat().st_ino))

    def test_constructor_owns_proxy_inspection_and_missing_fields_cannot_synthesize_success(self):
        for missing, partial in (
            ("NMRNET_FAKE_PROXY_STATE", b""),
            ("NMRNET_FAKE_PROXY_INSPECT_STATUS", b"true\n"),
        ):
            with (
                self.subTest(missing=missing),
                patch.dict(os.environ, {"NMRNET_FAKE_PROXY_STATE": "ambient-state", "NMRNET_FAKE_PROXY_INSPECT_STATUS": "37"}),
                tempfile.TemporaryDirectory() as directory,
            ):
                fixture = LauncherFixture(Path(directory))
                self.assertEqual("true", fixture.environment["NMRNET_FAKE_PROXY_STATE"])
                self.assertEqual("0", fixture.environment["NMRNET_FAKE_PROXY_INSPECT_STATUS"])
                proxy = "c" * 64
                ready = fixture.root / "ready"
                ready.mkdir()
                created = self.inspect(
                    fixture,
                    [
                        "create",
                        "--cidfile",
                        str(fixture.root / "proxy.cid"),
                        "--mount",
                        f"type=bind,src={ready},dst=/ready",
                        "fixture-image",
                    ],
                    {"NMRNET_FAKE_CID": proxy, "NMRNET_FAKE_ENGINE_STATUS": "0"},
                )
                self.assertEqual(0, created.returncode, msg=created.stderr)
                self.assertEqual((proxy + "\n").encode(), created.stdout)
                registry = fixture.root / "containers.json"
                before = (registry.read_bytes(), registry.stat().st_ino)
                arguments = ["inspect", "--format", "{{.State.Running}}", proxy]
                ordinary = self.inspect(fixture, arguments, {})
                self.assertEqual(0, ordinary.returncode, msg=ordinary.stderr)
                self.assertEqual(b"true\n", ordinary.stdout)
                self.assertEqual(b"", ordinary.stderr)
                for state, status in (("false", "0"), ("true", "37"), ("garbage", "0")):
                    fault = self.inspect(
                        fixture, arguments, {"NMRNET_FAKE_PROXY_STATE": state, "NMRNET_FAKE_PROXY_INSPECT_STATUS": status}
                    )
                    self.assertEqual(int(status), fault.returncode, msg=fault.stderr)
                    self.assertEqual((state + "\n").encode(), fault.stdout)
                    self.assertEqual(b"", fault.stderr)
                del fixture.environment[missing]
                absent = self.inspect(fixture, arguments, {})
                self.assertEqual(1, absent.returncode)
                self.assertEqual(partial, absent.stdout)
                self.assertIn(("KeyError: '" + missing + "'").encode(), absent.stderr)
                self.assertEqual(before, (registry.read_bytes(), registry.stat().st_ino))

    def test_missing_owned_metadata_cannot_synthesize_success_or_override_fault_status(self):
        for missing in ("NMRNET_FAKE_IMAGE_ID", "NMRNET_FAKE_IMAGE_REVISION"):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as directory:
                fixture = LauncherFixture(Path(directory))
                del fixture.environment[missing]
                label = '{{ index .Config.Labels "io.nmrnet-repro.upstream-revision" }}'
                arguments = ["image", "inspect", "--format", "{{.Id}}|" + label, "sha256:" + "b" * 64]
                result = self.inspect(fixture, arguments, {})
                self.assertNotEqual(0, result.returncode)
                self.assertEqual(b"", result.stdout)
                self.assertIn(b"KeyError", result.stderr)
                self.assertIn(missing.encode(), result.stderr)
                private = "explicit-inspection-failure"
                result = self.inspect(
                    fixture,
                    arguments,
                    {"NMRNET_FAKE_INSPECT_STATUS": "7", "NMRNET_FAKE_INSPECT_PRIVATE_ERROR": private},
                )
                self.assertEqual(7, result.returncode)
                self.assertEqual(b"", result.stdout)
                self.assertEqual((private + "\n").encode(), result.stderr)

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
