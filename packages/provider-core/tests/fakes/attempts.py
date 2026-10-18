"""Attempt read fixtures from the API revision cited in attempt_response.py."""

import json

ATTEMPT_REF = "execution_attempt:sha256:" + "a" * 64


def observation(**changes):
    document = {
        "schema_id": "nmr.provider.execution_attempt_read_response.v1",
        "execution_attempt_ref": ATTEMPT_REF,
        "job_ref": "job:example",
        "state": "in_progress",
        "job_state": "open",
    }
    document.update(changes)
    return json.dumps(document).encode()


def start_response(**changes):
    return json.dumps(
        {
            "schema_id": "nmr.provider.execution_attempt_start_response.v1",
            "execution_attempt_ref": ATTEMPT_REF,
            "job_ref": "job:example",
            "analysis_kind_ref": "nmr_predict",
            "provider_ref": "provider:example",
            "state": "in_progress",
            "started_at": "2026-10-18T00:00:00Z",
            "replayed": False,
        }
        | changes
    ).encode()


def retained_start(**changes):
    # Noncompact ordering/whitespace are intentional: recovery must resend bytes.
    return (
        json.dumps(
            {
                "provider_attempt_key": "retained-start:01",
                "job_ref": "job:example",
                "schema_id": "nmr.provider.execution_attempt_start_request.v1",
            }
            | changes,
            indent=2,
        ).encode()
        + b"\n"
    )
