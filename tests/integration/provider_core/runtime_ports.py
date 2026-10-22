"""Local signed discovery, source and model peers; science remains recording.

Reuses job-feed/signature fixtures from provider-core's test_job_discovery.py
and preparation ports from test_nmrnet_interpreter.py, at c452e23. These peers
exercise real HTTPS and the installed core, not a deployed API or external model.
"""

import base64
import hashlib
import json
import tempfile
from contextlib import AsyncExitStack
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from http_sf import parse
from tests.fakes.acquisition import store_headers
from tests.fakes.api_tls import api_tls_peer
from tests.fakes.attempts import ATTEMPT_REF, complete_response, fail_response, observation, progress_response, start_response
from tests.fakes.job_feed import page
from tests.fakes.job_input import response
from tests.fakes.tls_peer import TlsPeer
from tests.fakes.uploads import UPLOAD_REF, encoded, grant, member, membership
from tests.integration.nmrnet.config_ports import ConfigFiles
from tests.integration.nmrnet.interpreter_ports import tool_reply

from nmrnet_provider.config import ANALYSIS_KIND_REF, load_provider_config
from provider_core.api import ProviderAPI
from provider_core.attempt_record import StartPending
from provider_core.attempt_request import start_body
from provider_core.attempt_store import AttemptStore
from provider_core.execution import ObservationRecovery
from provider_core.ownership import AttemptClaims


def require_api_credentials(request):
    if b"bearer " in request.lower() or b"PRIVATE_MODEL_KEY" in request:
        raise AssertionError("API received a source or model bearer")


class RuntimePeers:
    def __init__(self, ports, route, *, maximum_records=1):
        self.ports, self.route = ports, route
        self.maximum_records = maximum_records
        self.claims = AttemptClaims()
        self.result = None
        self.corrupt_input = False
        self.stack = AsyncExitStack()

    async def __aenter__(self):
        try:
            self.api_peer = await self.stack.enter_async_context(api_tls_peer("success", response_factory=self.api_reply))
            self.model_peer = await self.stack.enter_async_context(TlsPeer("success", response_factory=self.model_reply))
            headers = tuple((name, value) for name, value in store_headers(self.ports.source) if name != b"content-length")
            self.store_peer = await self.stack.enter_async_context(
                TlsPeer("success", response_body=self.ports.source, response_headers=headers)
            )
            directory = self.stack.enter_context(tempfile.TemporaryDirectory())
            self.files = ConfigFiles(
                directory,
                api_origin=self.api_peer.origin,
                model_origin=self.model_peer.origin,
                store_origin=self.store_peer.origin,
                ca_file=Path(self.api_peer.scratch.name) / "cert.pem",
                model_ca_file=Path(self.model_peer.scratch.name) / "cert.pem",
                store_ca_file=Path(self.store_peer.scratch.name) / "cert.pem",
                maximum_records=self.maximum_records,
            )
            self.config = load_provider_config(self.files.path)
            self.files.state.mkdir(mode=0o700)
            settings = self.config.lifecycle
            self.store = AttemptStore(
                settings.store_root, scope=settings.scope, limits=settings.store_limits, operation_seconds=settings.store_seconds
            )
            self.stack.push_async_callback(self.store.close)
            await self.store.open()
            self.stack.push_async_callback(self.settle_store)
            self.api = ProviderAPI(
                self.config.api.identity,
                self.config.api.http.new_transport(),
                expected_topology=self.config.api.expected_topology,
            )
            self.stack.push_async_callback(self.api.retire)
            return self
        except BaseException:
            await self.stack.aclose()
            raise

    async def __aexit__(self, *error):
        return await self.stack.__aexit__(*error)

    async def settle_store(self):
        if self.store.pending_operation is not None:
            await self.store.settle()

    async def discover(self):
        discovered = await self.api.jobs(
            analysis_kind_ref=ANALYSIS_KIND_REF, has_provider_execution_attempt=False, limit=1, cursor=None
        )
        (selected,) = discovered.jobs
        return StartPending(
            self.config.lifecycle.scope,
            selected.analysis_kind_ref,
            selected.input,
            start_body(selected.input.job_ref, "runtime:one"),
        )

    def arguments(self, pending):
        return {
            "setup_owner": None,
            "config": self.config,
            "pending": pending,
            "api": self.api,
            "store": self.store,
            "claims": self.claims,
            "worker": self.ports.worker,
            "retirement": self.ports.host,
            "execution_identity": {"image_id": "fixture"},
            "selected_artifacts": self.ports.worker.artifacts,
            "runtime_expectations": self.ports.worker.runtime,
            "allocate_reader": self.ports.allocate_reader,
            "allocate_generator": self.ports.allocate_generator,
            "observation_recovery": ObservationRecovery(
                self.config.lifecycle.observation_gap_seconds,
                self.config.service.policy.api_backoff_max_seconds,
                self.observation_retry,
                self.observation_recovered,
            ),
            # Direct assembly keeps the explicit stopped-recovery qualification;
            # production's factory requires an enabled policy before allocation.
            "terminal_recovery": None,
        }

    def observation_retry(self, _attempt, _failure, _seconds):
        raise AssertionError("This unchanged scientific witness expects no observation outage")

    def observation_recovered(self, _attempt, _failure):
        raise AssertionError("This unchanged scientific witness expects no observation recovery")

    async def api_reply(self, request, body):
        method, target, _ = request.split(b"\r\n", 1)[0].decode().split(" ")
        path = urlsplit(target).path
        require_api_credentials(request)
        if path == "/provider/v1/jobs":
            document = page()
            document.update(analysis_kind_ref=ANALYSIS_KIND_REF, next_cursor=None)
            document["jobs"] = [
                {
                    **asdict(self.ports.instructions.identity),
                    "analysis_kind_ref": ANALYSIS_KIND_REF,
                    "created_at": "2026-10-18T00:00:00.123456Z",
                }
            ]
            return json.dumps(document).encode()
        if path.endswith("/input"):
            payload = b"PRIVATE changed after discovery" if self.corrupt_input else self.ports.instructions.specification_bytes
            return response(payload)
        if path.endswith("/uploads"):
            return encoded(
                membership(
                    uploads=[
                        member(
                            byte_length=len(self.ports.source),
                            content_hash="sha256:" + hashlib.sha256(self.ports.source).hexdigest(),
                        )
                    ]
                )
            )
        if path.endswith("/read-capability"):
            return encoded(
                grant(
                    byte_length=len(self.ports.source),
                    content_hash="sha256:" + hashlib.sha256(self.ports.source).hexdigest(),
                    download_url=self.store_peer.origin + "/upload/v1/uploads/" + UPLOAD_REF + "/bytes",
                    expires_at=(datetime.now(UTC) + timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
                )
            )
        if path.endswith("/start"):
            return start_response(analysis_kind_ref=ANALYSIS_KIND_REF)
        if path == "/provider/v1/execution-attempts/" + ATTEMPT_REF:
            return observation()
        if path.endswith("/progress"):
            return progress_response(phase=json.loads(body)["phase"])
        if path.endswith("/fail"):
            failure = json.loads(body)
            return fail_response(failure_code=failure["failure_code"], failure_message=failure["failure_message"])
        if path.endswith("/complete"):
            content = base64.b64decode(json.loads(body)["canonical_result_base64"], validate=True)
            self.result = json.loads(content)
            return complete_response(
                result_schema_id=self.result["schema_id"],
                result_byte_length=len(content),
                result_fingerprint="sha256:" + hashlib.sha256(content).hexdigest(),
            )
        raise AssertionError(f"Unexpected fixture route {method} {path}")

    async def model_reply(self, request, raw):
        if b"authorization: bearer private_model_key\r\n" not in request.lower():
            raise AssertionError("Model did not receive only its configured key")
        if b"signature:" in request.lower():
            raise AssertionError("Model received API signing headers")
        last = json.loads(raw)["messages"][-1]
        if self.route == "smiles":
            return tool_reply("prepare", "prepare_smiles", {"smiles": "CCl", "requested_count": 2})
        if self.route == "pasted":
            return tool_reply(
                "prepare",
                "prepare_pasted",
                {
                    "source_text": self.ports.source_text,
                    "source_format": "table",
                    "source_unit": "nm",
                },
            )
        if last["role"] != "tool":
            return tool_reply("inspect", "inspect_uploads", {})
        return tool_reply(
            "prepare",
            "prepare_upload",
            {
                "upload_ref": UPLOAD_REF,
                "source_format": "xyz",
                "source_unit": "angstrom",
                "selection": [2, 1],
                "alternate_location": None,
            },
        )

    def verify_discovery_signature(self):
        request = self.api_peer.requests[0]
        target = request.split(b"\r\n", 1)[0].split(b" ")[1]
        headers = {
            name.lower(): value.strip()
            for line in request.split(b"\r\n")[1:]
            if b":" in line
            for name, value in [line.split(b":", 1)]
        }
        path, query = target.split(b"?", 1)
        base = b"\n".join(
            [
                b'"@method": GET',
                b'"@authority": ' + self.api_peer.origin.removeprefix("https://").encode(),
                b'"@path": ' + path,
                b'"@query": ?' + query,
                b'"@signature-params": ' + headers[b"signature-input"].split(b"=", 1)[1],
            ]
        )
        self.config.api.identity.private_key.public_key().verify(
            parse(headers[b"signature"], tltype="dictionary")["sig1"][0], base
        )
