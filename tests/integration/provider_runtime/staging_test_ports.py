"""Disposable individual inputs for the offline staging contract tests."""

from tools.container_guard import require_container

require_container()

import json
import ssl
from pathlib import Path

from tests.fakes.credential import document, encoded


def staging_inputs(root):
    inputs, source, selection = root / "input", root / "source", root / "selection"
    for directory in (inputs, source, selection):
        directory.mkdir(mode=0o700)
    ca = Path(ssl.get_default_verify_paths().cafile).read_bytes()
    (inputs / "api-ca.crt").write_bytes(ca)
    (inputs / "provider.json").write_bytes(encoded(document()))
    (inputs / "provider.json").chmod(0o600)
    key = inputs / "selected.key"
    fragment = inputs / "interpreter.toml"
    fragment.write_text(
        "[interpreter]\nmax_turns = 6\n\n[[interpreter.endpoints]]\n"
        'configuration_id = "selected-fixture"\nurl = "https://model.example/chat/completions"\n'
        'model = "selected-model"\nreasoning_effort = "none"\n'
        f"key_file = {json.dumps(str(key))}\n"
        "request_bytes = 65536\nresponse_bytes = 262144\nexchange_seconds = 25\nretirement_seconds = 5\n"
    )
    fragment.chmod(0o600)
    return inputs, selection, fragment, key
