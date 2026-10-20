"""Controlled authenticated replies shaped from the API revision in core README."""

from provider_core.transport import ExchangeFailed, HttpResponse


class Transport:
    def __init__(self, *replies):
        self.replies = iter(replies)
        self.calls = []
        self.last_failure = None
        self.retirement_confirmed = True
        self.pending_owner = None

    async def retire(self, *, owner=None):
        if not self.retirement_confirmed and self.pending_owner is not owner:
            raise ExchangeFailed("Foreign exchange owner")
        self.retirement_confirmed = True
        self.pending_owner = None

    async def exchange(self, request, *, maximum_bytes, owner=None):
        if not self.retirement_confirmed:
            self.pending_owner = owner
        self.calls.append((request, maximum_bytes))
        result = next(self.replies)
        if isinstance(result, BaseException):
            raise result
        return result


def reply(body, *, status=200, headers=None):
    return HttpResponse(
        status,
        headers
        if headers is not None
        else response_headers(status=status, request_id=b"request:test" if status != 200 else None),
        body,
    )


def response_headers(*, topology=b"dev-local", request_id=None, status=200):
    """Authored valid API metadata; explicit adversarial headers stay untouched."""
    headers = (
        (b"content-type", b"application/json" if status == 200 else b"application/problem+json"),
        (b"cache-control", b"no-store"),
        (b"nmr-api-topology", topology),
    )
    return headers if request_id is None else (*headers, (b"x-request-id", request_id))
