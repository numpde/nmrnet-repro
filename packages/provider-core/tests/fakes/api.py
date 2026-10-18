"""Controlled authenticated replies shaped from the API revision in core README."""

from provider_core.transport import HttpResponse


class Transport:
    def __init__(self, *replies):
        self.replies = iter(replies)
        self.calls = []
        self.last_failure = None

    async def exchange(self, request, *, maximum_bytes):
        self.calls.append((request, maximum_bytes))
        result = next(self.replies)
        if isinstance(result, BaseException):
            raise result
        return result


def reply(body, *, status=200, headers=None):
    return HttpResponse(
        status,
        headers if headers is not None else ((b"content-type", b"application/json"), (b"cache-control", b"no-store")),
        body,
    )
