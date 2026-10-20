"""Normal startup reads explicit files into the actual existing runtime policies."""

import hashlib
import json
import ssl
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.fakes.tls_peer import TlsPeer
from tests.integration.nmrnet.config_ports import ConfigFiles

from nmrnet_provider import config as config_module
from nmrnet_provider.config import load_provider_config
from provider_core.attempt_record import MAX_RECORD_BYTES
from provider_core.chat import ChatEndpoint
from provider_core.uploads import UploadLimits


class ProviderConfigurationTests(unittest.IsolatedAsyncioTestCase):
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
                read_file = config_module._read_file

                def replace_path_after_read(path, **options):
                    content = read_file(path, **options)
                    if options["label"] == "interpreter CA":
                        Path(path).write_bytes(b"PRIVATE replacement is not a certificate")
                    return content

                with patch("nmrnet_provider.config._read_file", side_effect=replace_path_after_read):
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
