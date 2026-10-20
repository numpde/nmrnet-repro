"""Explicit disposable startup files; their credentials have no deployed authority."""

import json
from pathlib import Path

from tests.fakes.credential import document, encoded


class ConfigFiles:
    def __init__(
        self,
        root,
        *,
        api_origin="https://api.example",
        model_origin="https://model.example",
        store_origin="https://store.example",
        ca_file=None,
    ):
        self.root = Path(root)
        self.credential = self.root / "signing.private.json"
        self.key = self.root / "interpreter.key"
        self.path = self.root / "provider.toml"
        self.state = self.root / "state"
        self.credential.write_bytes(encoded(document()))
        self.credential.chmod(0o600)
        self.key.write_bytes(b"PRIVATE_MODEL_KEY\n")
        self.key.chmod(0o600)
        ca = "" if ca_file is None else "ca_file = " + json.dumps(str(ca_file)) + "\n"
        self.raw = f"""schema_id = "nmrnet.provider.runtime_config.v1"

[api]
origin = {json.dumps(api_origin)}
profile = "dev-local"
provider_ref = "provider:example"
credential_file = {json.dumps(str(self.credential))}
exchange_seconds = 2
retirement_seconds = 1
{ca}
[interpreter]
max_turns = 6

[[interpreter.endpoints]]
configuration_id = "primary"
url = {json.dumps(model_origin + "/chat")}
model = "fixture-model"
key_file = {json.dumps(str(self.key))}
request_bytes = 65536
response_bytes = 16384
exchange_seconds = 2
retirement_seconds = 1
{ca}
[uploads]
store_origin = {json.dumps(store_origin)}
per_source_bytes = 1048576
total_bytes = 2097152
requests = 8
seconds = 10
exchange_seconds = 2
retirement_seconds = 1
{ca}
[preparation]
seed = 17
max_iterations = 200
timeout_seconds = 1
generation_seconds = 5
max_worker_calls = 2
seconds = 45

[lifecycle]
consumer_id = "lane:fixture"
generation_id = "generation:stable-fixture"
store_root = {json.dumps(str(self.state))}
maximum_records = 1
filesystem_reserve_bytes = 0
store_seconds = 2
allowed_job_states = ["open"]
poll_seconds = 0.2
task_settle_seconds = 1
""".encode()
        self.write(self.raw)

    def write(self, raw):
        self.path.write_bytes(raw)
        self.path.chmod(0o600)
