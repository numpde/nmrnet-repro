"""Extend the existing configured TLS peers with authored Job feed pages."""

import json
from urllib.parse import urlsplit

from tests.fakes.job_feed import page as feed_page
from tests.integration.nmrnet.runtime_ports import RuntimePeers, require_api_credentials


def feed_response(**changes):
    return json.dumps(feed_page() | {"analysis_kind_ref": "peaks_from_mol", "jobs": []} | changes).encode()


class AdmissionPeers(RuntimePeers):
    def __init__(self, ports, *, maximum_live_attempts=2):
        super().__init__(ports, "smiles", maximum_live_attempts=maximum_live_attempts)
        self.feed_replies = None
        self.request_paths = []

    async def api_reply(self, request, body):
        require_api_credentials(request)
        target = request.split(b"\r\n", 1)[0].split(b" ")[1].decode()
        path = urlsplit(target).path
        self.request_paths.append(path)
        if path == "/provider/v1/execution-attempts":
            raise AssertionError("Live admission must not read prior Attempt history")
        if path == "/provider/v1/jobs" and self.feed_replies is not None:
            return self.feed_replies.pop(0)
        return await super().api_reply(request, body)
