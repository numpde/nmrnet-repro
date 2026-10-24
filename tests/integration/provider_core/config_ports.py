"""Explicit disposable startup files; their credentials have no deployed authority."""

import hashlib
import json
from pathlib import Path

from tests.fakes.credential import document, encoded


def ca_line(path):
    return "" if path is None else "ca_file = " + json.dumps(str(path)) + "\n"


def native_capture_files(root):
    """A fixed two-file native capture, independent of the launch producer."""
    root = Path(root) / "native-inputs"
    (root / "sources" / "tools").mkdir(parents=True, mode=0o700)
    (root / "sources" / "requirements").mkdir(mode=0o700)
    names = ("requirements/inference.lock", "tools/reader.py", "tools/worker.py")
    rows = []
    for name, content in zip(names, (b"lock entry\n" * 10000, b"reader source\n", b"worker source\n"), strict=True):
        (root / "sources" / name).write_bytes(content)
        rows.append(hashlib.sha256(content).hexdigest() + "  " + name + "\n")
    (root / "inputs.paths").write_text("\n".join(names) + "\n")
    (root / "sources.sha256").write_text("".join(rows))
    (root / "revision").write_text("a" * 40 + "\n")
    return root


class ConfigFiles:
    def __init__(
        self,
        root,
        *,
        api_origin="https://api.example",
        model_origin="https://model.example",
        store_origin="https://store.example",
        ca_file=None,
        model_ca_file=None,
        store_ca_file=None,
        maximum_records=1,
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
        ca = ca_line(ca_file)
        model_ca = ca if model_ca_file is None else ca_line(model_ca_file)
        store_ca = ca if store_ca_file is None else ca_line(store_ca_file)
        self.raw = f"""schema_id = "nmrnet.provider.runtime_config.v1"

[api]
origin = {json.dumps(api_origin)}
profile = "dev-local"
topology = "dev-local"
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
{model_ca}
[uploads]
store_origin = {json.dumps(store_origin)}
per_source_bytes = 1048576
total_bytes = 2097152
requests = 8
seconds = 10
exchange_seconds = 2
retirement_seconds = 1
{store_ca}
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
maximum_records = {maximum_records}
confirmed_history_records = 64
filesystem_reserve_bytes = 0
store_seconds = 2
allowed_job_states = ["open"]
poll_seconds = 0.2
task_settle_seconds = 1
observation_gap_seconds = 30

[service]
display_name = "NMRNet local fixture"
description = "NMRNet coordinate shifts"
offering_description = "Coordinate shifts"
page_size = 1
maximum_pages = 2
idle_seconds = 0.2
api_backoff_max_seconds = 30
hello_seconds = 2
drain_seconds = 1
settle_seconds = 5
allocation_seconds = 30
prediction_seconds = 90
source_seconds = 10
""".encode()
        self.write(self.raw)

    def write(self, raw):
        self.path.write_bytes(raw)
        self.path.chmod(0o600)
