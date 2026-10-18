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
