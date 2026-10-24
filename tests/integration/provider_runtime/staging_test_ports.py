"""Disposable individual inputs for the offline staging contract tests."""

from tools.container_guard import require_container

require_container()

import base64
import hashlib
import json
import ssl
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from stage_api_config import inspect_interpreter, stage
from tests.fakes.credential import document, encoded

from nmrnet_provider.config import load_provider_config
from provider_core.transport import HttpResponse
from provider_core.upload_response import ReadGrant, UploadMember, UploadSet
from provider_core.uploads import JobUploads


def staging_inputs(root):
    inputs, source, selection = root / "input", root / "source", root / "selection"
    for directory in (inputs, source, selection):
        directory.mkdir(mode=0o700)
    ca = Path(ssl.get_default_verify_paths().cafile).read_bytes()
    (inputs / "api-ca.crt").write_bytes(ca)
    (inputs / "provider.json").write_bytes(encoded(document()))
    (inputs / "provider.json").chmod(0o600)
    key = inputs / "interpreter.key"
    fragment = inputs / "interpreter.toml"
    fragment.write_text(
        "[interpreter]\nmax_turns = 6\n\n[[interpreter.endpoints]]\n"
        'configuration_id = "selected-fixture"\nurl = "https://model.example/chat/completions"\n'
        'model = "selected-model"\nreasoning_effort = "none"\n'
        f"key_file = {json.dumps(str(root / 'unmounted-selection/model.key'))}\n"
        "request_bytes = 65536\nresponse_bytes = 262144\nexchange_seconds = 25\nretirement_seconds = 5\n"
    )
    fragment.chmod(0o600)
    return inputs, selection, fragment, key


def staged_real_config(root):
    """Admit the real staging profile using only synthetic private inputs."""
    inputs, selection, fragment, key = staging_inputs(root)
    key.write_bytes(b"synthetic-fixture-key\n")
    key.chmod(0o600)
    inspect_interpreter(fragment, selection)
    (inputs / "interpreter-inputs.json").write_bytes((selection / "interpreter-inputs.json").read_bytes())
    stage(root, "https://api.example:9443", "dev-local", inputs=inputs, interpreter_config=fragment, prediction_image_id="sha256:" + "c" * 64)
    return load_provider_config(root / "source/provider.toml")


def upload_sources(limits):
    """Give the actual acquisition owner finite observed API/store replies."""
    body = b"synthetic coordinates"
    upload_ref = "upload:sha256:" + "a" * 64
    digest = "sha256:" + hashlib.sha256(body).hexdigest()
    member = UploadMember(upload_ref, "synthetic XYZ fixture", len(body), digest)
    grant = ReadGrant(
        "job:example",
        upload_ref,
        len(body),
        digest,
        "https://store.example/upload/v1/uploads/" + upload_ref + "/bytes",
        "synthetic-read-bearer",
        datetime.now(UTC) + timedelta(minutes=5),
    )
    api = SimpleNamespace(
        uploads=AsyncMock(return_value=UploadSet("job:example", (member,))),
        read_grant=AsyncMock(return_value=grant),
        last_failure=None,
    )

    async def exchange(_request, *, maximum_bytes, account_bytes):
        assert maximum_bytes == len(body)
        account_bytes(len(body))
        return HttpResponse(
            200,
            (
                (b"content-type", b"application/octet-stream"),
                (b"cache-control", b"no-store"),
                (b"content-length", str(len(body)).encode()),
                (b"content-digest", b"sha-256=:" + base64.b64encode(hashlib.sha256(body).digest()) + b":"),
            ),
            body,
        )

    store = SimpleNamespace(exchange=AsyncMock(side_effect=exchange), retirement_confirmed=True, last_failure=None)
    owner = JobUploads("job:example", api=api, store=store, store_origin="https://store.example", limits=limits)
    return owner, api, store, upload_ref
