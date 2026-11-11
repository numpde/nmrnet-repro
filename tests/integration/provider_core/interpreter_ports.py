"""Script only model replies; use the actual installed HTTPS chat adapter."""

import hashlib
import json
from dataclasses import replace

from tests.fakes.attempt_store import admission, record
from tests.fakes.job_input import facts, response
from tests.fakes.uploads import UPLOAD_REF
from tests.integration.nmrnet.smiles_ports import SessionPorts

from nmrnet_provider.interpreter import InputInterpreter
from nmrnet_provider.xyz import prepare_xyz
from provider_core.attempt_record import acknowledge_start
from provider_core.chat import ChatEndpoint, ChatEndpointSpec
from provider_core.job_input import verify_job_input
from provider_core.transport import HttpTransport
from provider_core.uploads import VerifiedUpload


class UploadPorts(SessionPorts):
    """Unlike authored records make order, selected bytes and source identity observable."""

    def __init__(self):
        super().__init__()
        raw = b"Predict attached XYZ records 2 then 1, in angstrom; keep supplied atoms."
        self.instructions = verify_job_input(response(raw), feed=facts(raw))
        self.source = b"1\nAuthored oxygen\nO 0.1 0 0\n1\nAuthored hydrogen\nH 1.2 0 0\n"
        self.uploads.acquire.return_value = VerifiedUpload(
            "job:example",
            UPLOAD_REF,
            len(self.source),
            "sha256:" + hashlib.sha256(self.source).hexdigest(),
            self.source,
        )
        self.reader.read.side_effect = self.read
        self.pending = replace(record(), input=self.instructions.identity)
        self.acknowledged = acknowledge_start(self.pending, admission())

    async def read(self, source, **options):
        return prepare_xyz(source, source_unit=options["source_unit"], selection=options["selection"])


def tool_reply(identifier, name, arguments):
    message = {
        "role": "assistant",
        "reasoning_content": "fixture reasoning",
        "tool_calls": [
            {
                "id": identifier,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ],
    }
    return json.dumps({"choices": [{"message": message, "finish_reason": "tool_calls"}]}).encode()


def chat_endpoint(peer, *, identifier="fixture", request_bytes=65536, exchange_seconds=1):
    return ChatEndpoint(
        ChatEndpointSpec(
            configuration_id=identifier,
            model="fixture-model",
            url=peer.origin + "/chat",
            api_key="PRIVATE_MODEL_KEY",
            reasoning_effort=None,
        ),
        transport=HttpTransport(trust=peer.trust, exchange_seconds=exchange_seconds, retirement_seconds=0.5),
        request_bytes=request_bytes,
        response_bytes=16384,
    )


def fixture_interpreter(endpoints, *, timeout_seconds, on_endpoint_failure=None):
    """Give each scripted visit its own preconstructed fixture carrier."""
    factories = tuple((lambda *, exchange_seconds, connect_seconds, endpoint=endpoint: endpoint)
                      for endpoint in endpoints)
    return InputInterpreter(factories, timeout_seconds=timeout_seconds, context_bytes=60000,
                            secrets=tuple(endpoint.spec.api_key for endpoint in endpoints),
                            on_endpoint_failure=on_endpoint_failure)
