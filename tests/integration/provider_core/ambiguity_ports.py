"""Scripted model decisions for offline probe mechanics, never semantic evidence."""

import json
from types import SimpleNamespace

from stage_api_config import inspect_interpreter
from tests.fakes.conversation import call, endpoint

from nmrnet_provider.config import _interpreter


def settings(root, *, exchange_seconds=45, retirement_seconds=45):
    key = root / "model.key"
    key.write_bytes(b"PRIVATE_PROBE_KEY\n")
    key.chmod(0o600)
    table = {
        "max_turns": 6,
        "endpoints": [
            {
                "configuration_id": "authored",
                "url": "https://model.example/chat",
                "model": "authored-model",
                "key_file": str(key),
                "request_bytes": 65536,
                "response_bytes": 262144,
                "exchange_seconds": exchange_seconds,
                "retirement_seconds": retirement_seconds,
            }
        ],
    }
    from nmrnet_provider.config import read_startup_file  # noqa: PLC0415 -- This fixture uses the real key admission.

    return _interpreter(table, read_startup_file)


def scripted_carrier(case, *, issue=None, prepare=False, prepare_name="prepare_smiles", prepare_arguments=None, operational=None):
    issue = case["expected_issue"] if issue is None else issue
    replies = []
    if case["attachment"] and not prepare:
        replies.append(call("inspect", "inspect_uploads", {}))
        replies.append(call("source", "inspect_upload", {"upload_ref": "upload:sha256:" + "1" * 64}))
    if prepare:
        if prepare_arguments is None:
            prepare_arguments = {"smiles": "CCl", "requested_count": 1}
        replies.append(call("prepare", prepare_name, prepare_arguments))
    replies.append(
        call(
            "report",
            "report_input_problem",
            {"issue": issue, "message": "Useful private explanation. Bearer DISTINCT_REPORT_CANARY"},
        )
    )
    carrier = endpoint("authored", *replies)
    carrier.spec = SimpleNamespace(api_key="PRIVATE_PROBE_KEY")
    if operational is not None:
        carrier.complete.side_effect = operational
    return carrier


def format_carrier(case, *, changes=None):
    """Fixed replies exercise argument capture, never real semantic judgment."""
    arguments = {
        "upload_ref": "upload:sha256:" + "1" * 64,
        "source_format": case["source_format"],
        "source_unit": case["source_unit"],
        "selection": case["selection"],
        "alternate_location": None,
    }
    if changes is not None:
        arguments.update(changes)
    carrier = endpoint(
        "authored",
        call("inspect", "inspect_uploads", {}),
        call("file", "inspect_upload", {"upload_ref": "upload:sha256:" + "1" * 64}),
        call("choose", "prepare_upload", arguments),
    )
    carrier.spec = SimpleNamespace(api_key="PRIVATE_PROBE_KEY")
    return carrier


def selected_files(root):
    """An unmounted declared key path is bound to one explicit synthetic grant."""
    fragment, key, selection = root / "interpreter.toml", root / "granted.key", root / "selection"
    selection.mkdir(mode=0o700)
    fragment.write_text(
        "[interpreter]\nmax_turns = 6\n[[interpreter.endpoints]]\n"
        'configuration_id = "authored"\nurl = "https://model.example/chat"\nmodel = "authored-model"\n'
        f"key_file = {json.dumps(str(root / 'unmounted/model.key'))}\n"
        "request_bytes = 65536\nresponse_bytes = 262144\nexchange_seconds = 45\nretirement_seconds = 45\n"
    )
    fragment.chmod(0o600)
    key.write_bytes(b"PRIVATE_PROBE_KEY\n")
    key.chmod(0o600)
    inspect_interpreter(fragment, selection)
    return fragment, key, selection / "interpreter-inputs.json"
