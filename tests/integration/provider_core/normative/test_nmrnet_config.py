"""Normal startup reads explicit files into the actual existing runtime policies."""

import hashlib
import io
import json
import ssl
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.fakes.tls_peer import TlsPeer
from tests.integration.nmrnet.config_ports import ConfigFiles, native_capture_files, startup_capture_files

from nmrnet_provider import config as config_module
from nmrnet_provider.config import _read_provider_config, load_provider_config, read_startup_file
from nmrnet_provider.launch_capture import capture_execution, host_fields
from nmrnet_provider.launch_capture import main as launch_main
from nmrnet_provider.native_factory import NativePolicy
from nmrnet_provider.service import Announcement, ServicePolicy
from nmrnet_provider.startup_capture import capture_startup
from provider_core.attempt_record import MAX_RECORD_BYTES
from provider_core.chat import ChatEndpoint
from provider_core.hello import hello_body
from provider_core.uploads import UploadLimits


class ProviderConfigurationTests(unittest.IsolatedAsyncioTestCase):
    def test_launch_cli_emits_multiple_authorities_only_after_complete_capture(self):
        for changed in (False, True):
            with self.subTest(changed_source=changed), tempfile.TemporaryDirectory() as directory:
                files, snapshot = startup_capture_files(directory)
                endpoint = files.raw.split(b"[[interpreter.endpoints]]", 1)[1].split(b"[uploads]", 1)[0]
                second = endpoint.replace(b'"primary"', b'"secondary"').replace(b"model.example", b"other-model.example")
                files.write(files.raw.replace(b"[uploads]", b"[[interpreter.endpoints]]" + second + b"[uploads]"))
                native = native_capture_files(directory)
                if changed:
                    (native / "sources/tools/worker.py").write_bytes(b"changed\n")
                arguments = [
                    "--config",
                    str(files.path),
                    "--source-root",
                    str(files.root),
                    "--destination",
                    str(snapshot),
                    "--maximum-bytes",
                    "65536",
                    "--native-capture",
                    str(native),
                    "--image-id",
                    "sha256:" + "b" * 64,
                    "--geometry-image-id",
                    "sha256:" + "c" * 64,
                    "--controller-image-id",
                    "sha256:" + "d" * 64,
                    "--repository-revision",
                    "a" * 40,
                    "--upstream-revision",
                    "e" * 40,
                ]
                binary, errors = io.BytesIO(), io.StringIO()
                with patch("sys.stdout", io.TextIOWrapper(binary)), patch("sys.stderr", errors):
                    if changed:
                        with self.assertRaises(SystemExit) as stopped:
                            launch_main(arguments)
                        self.assertEqual(stopped.exception.code, 2)
                        self.assertEqual(binary.getvalue(), b"")
                        self.assertNotIn("PRIVATE_MODEL_KEY", errors.getvalue())
                        self.assertFalse((snapshot / "execution.json").exists())
                    else:
                        self.assertEqual(launch_main(arguments), 0)
                        fields = binary.getvalue().split(b"\0")
                        self.assertEqual(len(fields), 18)
                        self.assertEqual(fields[4], b"model.example:443\nother-model.example:443")
                        self.assertEqual(fields[-1], b"")
                        self.assertTrue((snapshot / "execution.json").is_file())

    def test_launch_handoff_has_exact_fields_and_execution_uses_the_frozen_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            files, snapshot = startup_capture_files(directory)
            plan = capture_startup(files.path, source_root=files.root, destination=snapshot, maximum_bytes=65536)
            native = native_capture_files(directory)
            identities = {
                "image_id": "sha256:" + "b" * 64,
                "geometry_image_id": "sha256:" + "c" * 64,
                "controller_image_id": "sha256:" + "d" * 64,
                "repository_revision": "a" * 40,
                "upstream_revision": "e" * 40,
            }
            capture_execution(native, snapshot, **identities)
            declaration = json.loads((snapshot / "execution.json").read_bytes())
            self.assertEqual({name: declaration[name] for name in identities}, identities)
            self.assertEqual(declaration["runtime_identity_source"], "caller")
            self.assertEqual(
                declaration["tool_sources"]["tools/worker.py"],
                {"size": 14, "sha256": hashlib.sha256(b"worker source\n").hexdigest()},
            )
            self.assertEqual(
                set(declaration["tool_sources"]), {"requirements/inference.lock", "tools/reader.py", "tools/worker.py"}
            )
            self.assertEqual(declaration["tool_sources"]["requirements/inference.lock"]["size"], 110000)
            fields = host_fields(plan).split(b"\0")
            self.assertEqual(len(fields), 18)
            self.assertEqual(fields[-1], b"")
            self.assertEqual(
                fields,
                [
                    str(files.root).encode(),
                    b"provider.toml",
                    hashlib.sha256(files.raw).hexdigest().encode(),
                    b"api.example:443",
                    b"model.example:443",
                    b"store.example:443",
                    str(Path(directory) / "state").encode(),
                    b"30",
                    b"90",
                    b"10",
                    b"5",
                    b"1",
                    b"17",
                    b"200",
                    b"2",
                    b"1",
                    b"5",
                    b"",
                ],
            )
            self.assertNotIn(files.key.read_bytes().strip(), host_fields(plan))
            self.assertEqual((snapshot / "execution.json").stat().st_mode & 0o777, 0o600)

    def test_capture_preserves_omitted_default_and_custom_destination_ports(self):
        for port, expected, roles in (
            ("", 443, ("api", "model", "store")),
            (":443", 443, ("api",)),
            (":8443", 8443, ("api", "model", "store")),
        ):
            with self.subTest(port=port), tempfile.TemporaryDirectory() as directory:
                files, snapshot = startup_capture_files(directory)
                raw = files.raw
                for role in roles:
                    raw = raw.replace(f"https://{role}.example".encode(), f"https://{role}.example{port}".encode())
                files.write(raw)
                plan = capture_startup(files.path, source_root=files.root, destination=snapshot, maximum_bytes=65536)
                self.assertEqual(
                    plan["authorities"],
                    {
                        "api": [f"api.example:{expected}"],
                        "interpreter": [f"model.example:{expected}"],
                        "store": [f"store.example:{expected}"],
                    },
                )

    def test_startup_capture_keeps_the_consumed_bytes_without_rereading_or_copying_other_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files, snapshot = startup_capture_files(root)
            source = files.root
            (source / "unrelated.private").write_bytes(b"UNGRANTED_SECRET")
            original_key = files.key.read_bytes()

            def consume_then_change(path, **settings):
                raw = read_startup_file(path, **settings)
                if Path(path) == files.key:
                    files.key.write_bytes(b"CHANGED_AFTER_READ\n")
                return raw

            expected_size = len(files.raw) + len(files.credential.read_bytes()) + len(original_key)
            with patch("nmrnet_provider.startup_capture.read_startup_file", side_effect=consume_then_change):
                plan = capture_startup(files.path, source_root=source, destination=snapshot, maximum_bytes=expected_size)
            self.assertEqual((snapshot / "inputs/provider.toml").read_bytes(), files.raw)
            self.assertEqual((snapshot / "inputs/interpreter.key").read_bytes(), original_key)
            self.assertEqual(
                sorted(path.name for path in (snapshot / "inputs").iterdir()),
                ["interpreter.key", "provider.toml", "signing.private.json"],
            )
            self.assertEqual(plan, json.loads((snapshot / "plan.json").read_bytes()))
            self.assertEqual(plan["config_sha256"], hashlib.sha256(files.raw).hexdigest())
            self.assertEqual(
                plan["authorities"],
                {"api": ["api.example:443"], "interpreter": ["model.example:443"], "store": ["store.example:443"]},
            )
            self.assertEqual(plan["native"]["source_seconds"], 10)
            self.assertEqual(plan["native"]["generation_seconds"], 5)
            self.assertEqual(plan["native"]["embedding_seconds"], 1)
            self.assertFalse((root / "state").exists())
            rendered = (snapshot / "plan.json").read_text()
            for secret in ("PRIVATE_MODEL_KEY", "CHANGED_AFTER_READ", "UNGRANTED_SECRET", "private_key"):
                self.assertNotIn(secret, rendered)

            def frozen_read(path, **settings):
                return read_startup_file(snapshot / "inputs" / Path(path).relative_to(source), **settings)

            frozen = _read_provider_config(files.path, frozen_read)
            self.assertEqual(frozen.source_sha256, plan["config_sha256"])
            self.assertEqual(frozen.interpreter.endpoints[0].spec.api_key, "PRIVATE_MODEL_KEY")
            for path in snapshot.rglob("*"):
                self.assertEqual(path.stat().st_mode & 0o777, 0o700 if path.is_dir() else 0o600)

    def test_service_presentation_and_policies_are_admitted_by_the_existing_owners(self):
        with tempfile.TemporaryDirectory() as directory:
            files = ConfigFiles(directory)
            configured = load_provider_config(files.path).service
            self.assertIs(type(configured.announcement), Announcement)
            self.assertIs(type(configured.policy), ServicePolicy)
            self.assertIs(type(configured.native), NativePolicy)
            self.assertEqual((configured.policy.admission.page_size, configured.policy.admission.maximum_pages), (1, 2))
            self.assertEqual(
                (
                    configured.policy.idle_seconds,
                    configured.policy.hello_seconds,
                    configured.policy.drain_seconds,
                    configured.policy.settle_seconds,
                ),
                (0.2, 2, 1, 5),
            )
            self.assertEqual(configured.native, NativePolicy(allocation_seconds=30, prediction_seconds=90, source_seconds=10))
            snapshot = configured.announcement
            body = json.loads(
                hello_body(
                    display_name=snapshot.display_name,
                    description=snapshot.description,
                    analysis_offerings=snapshot.analysis_offerings,
                )
            )
            self.assertEqual(body["display_name"], "NMRNet local fixture")
            self.assertEqual(
                body["analysis_offerings"], [{"analysis_kind_ref": "peaks_from_mol", "description": "Coordinate shifts"}]
            )
            self.assertFalse(files.state.exists())

    async def test_files_admit_original_identity_policies_and_explicit_tls_without_work(self):
        async with TlsPeer("success") as peer:
            with tempfile.TemporaryDirectory() as directory:
                files = ConfigFiles(
                    directory,
                    api_origin=peer.origin,
                    model_origin=peer.origin,
                    store_origin=peer.origin,
                    ca_file=Path(peer.scratch.name) / "cert.pem",
                )
                config = load_provider_config(files.path)
                self.assertEqual(config.api.identity.origin, peer.origin)
                self.assertEqual(config.api.expected_topology, "dev-local")
                self.assertEqual(config.api.identity.credential_ref, "credential:example")
                self.assertEqual(config.lifecycle.scope.provider_ref, "provider:example")
                self.assertEqual(config.lifecycle.scope.generation_id, "generation:stable-fixture")
                self.assertEqual(config.lifecycle.store_limits.recovery_record_bytes, MAX_RECORD_BYTES)
                self.assertIsInstance(config.uploads.limits, UploadLimits)
                self.assertEqual(config.preparation.generation_settings.seed, 17)
                self.assertEqual(config.source_sha256, hashlib.sha256(files.raw).hexdigest())
                self.assertFalse(files.state.exists())
                self.assertEqual(peer.requests, [])
                endpoint = config.interpreter.endpoints[0]
                self.assertEqual(endpoint.spec.api_key, "PRIVATE_MODEL_KEY")
                self.assertEqual(config.api.http.trust.cert_store_stats()["x509"], 1)
                for trust in (config.api.http.trust, config.uploads.http.trust, endpoint.http.trust):
                    self.assertEqual(trust.verify_mode, ssl.CERT_REQUIRED)
                    self.assertTrue(trust.check_hostname)
                self.assertNotIn("PRIVATE_MODEL_KEY", repr(config))
                self.assertNotIn(directory, repr(config))

    async def test_loaded_private_ca_is_the_exact_trust_used_by_the_actual_carrier(self):
        async with TlsPeer(
            "success", response_body=b'{"choices":[{"message":{"role":"assistant","content":"fixture"},"finish_reason":"stop"}]}'
        ) as peer:
            with tempfile.TemporaryDirectory() as directory:
                model_ca = Path(directory) / "model-ca.pem"
                model_ca.write_bytes((Path(peer.scratch.name) / "cert.pem").read_bytes())
                files = ConfigFiles(directory, model_origin=peer.origin)
                endpoint_header = "[[interpreter.endpoints]]\n"
                explicit_ca = "ca_file = " + json.dumps(str(model_ca)) + "\n"
                files.write(files.raw.replace(endpoint_header.encode(), (endpoint_header + explicit_ca).encode()))
                read_file = config_module.read_startup_file

                def replace_path_after_read(path, **options):
                    content = read_file(path, **options)
                    if options["label"] == "interpreter CA":
                        Path(path).write_bytes(b"PRIVATE replacement is not a certificate")
                    return content

                with patch("nmrnet_provider.config.read_startup_file", side_effect=replace_path_after_read):
                    config = load_provider_config(files.path)
                settings = config.interpreter.endpoints[0]
                transport = settings.http.new_transport()
                endpoint = ChatEndpoint(
                    settings.spec,
                    transport=transport,
                    request_bytes=settings.request_bytes,
                    response_bytes=settings.response_bytes,
                )
                self.addAsyncCleanup(endpoint.retire)
                reply = await endpoint.complete([{"role": "user", "content": "fixture"}], [])
                self.assertEqual(reply["content"], "fixture")
                self.assertEqual(len(peer.requests), 1)
