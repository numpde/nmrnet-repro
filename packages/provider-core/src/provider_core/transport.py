"""Own one bounded HTTPS exchange and its retirement before admitting another.

Each exchange uses a fresh client. Source precedent: Magnet provider_http._send
at the revision pinned in README. Unlike its best-effort response cleanup, failed
or unfinished cleanup here remains owned and blocks subsequent exchanges.
"""

from provider_core.container_guard import require_container

require_container()

import asyncio
import math
import ssl
from dataclasses import dataclass, field

import httpcore

from provider_core.network import OwnedNetworkBackend


class ExchangeFailed(RuntimeError):
    """Safe public failure text with original exceptions for protected diagnostics.

    primary and cleanup_failures may contain credentials or scientific data.
    Recipients must use a protected diagnostic sink, never render them directly.
    Transport failure does not establish whether a server mutation committed.
    """

    def __init__(self, message, *, primary=None, cleanup_failures=(), interruption=None):
        super().__init__(message)
        self.primary = primary
        self.cleanup_failures = tuple(cleanup_failures)
        self.interruption = interruption


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """Complete raw response bytes; no automatic decoding or status interpretation."""

    status: int
    headers: tuple[tuple[bytes, bytes], ...] = field(repr=False)
    body: bytes = field(repr=False)


@dataclass(slots=True, repr=False)
class _Outcome:
    response: HttpResponse | None = None
    primary: BaseException | None = None
    cleanup_failures: list[BaseException] = field(default_factory=list)


def _positive_seconds(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError("Cannot configure HTTPS exchange: time limits must be finite positive seconds")
    return value


class HttpTransport:
    """Serial exchange owner with a total deadline and additional cleanup grace.

    The composition owns this object and must await retire() before replacement
    or shutdown. An unconfirmed retirement requires enclosing process retirement;
    constructing another owner does not discharge the old owner's obligations.
    """

    def __init__(self, *, trust, exchange_seconds, retirement_seconds):
        if not isinstance(trust, ssl.SSLContext) or trust.verify_mode != ssl.CERT_REQUIRED or not trust.check_hostname:
            raise ValueError("Cannot configure HTTPS exchange: TLS must verify the certificate and hostname")
        self._trust = trust
        self._exchange_seconds = _positive_seconds(exchange_seconds)
        self._retirement_seconds = _positive_seconds(retirement_seconds)
        self._active = None
        self.last_failure = None

    async def _perform(self, request, maximum_bytes):
        outcome = _Outcome()
        client = None
        response = None
        network = OwnedNetworkBackend()
        try:
            client = httpcore.AsyncConnectionPool(
                ssl_context=self._trust,
                network_backend=network,
                max_connections=1,
                max_keepalive_connections=0,
                retries=0,
            )
            wire_request = httpcore.Request(
                method=request.method,
                url=httpcore.URL(
                    scheme=request.url.raw_scheme, host=request.url.raw_host, port=request.url.port, target=request.url.raw_path
                ),
                headers=request.headers.raw,
                content=request.content,
                extensions={"timeout": dict.fromkeys(("connect", "read", "write", "pool"), self._exchange_seconds)},
            )
            response = await client.handle_async_request(wire_request)
            body = bytearray()
            async for chunk in response.aiter_stream():
                if len(body) + len(chunk) > maximum_bytes:
                    raise ExchangeFailed(f"Cannot receive HTTPS response: raw body exceeds the {maximum_bytes}-byte limit")
                body.extend(chunk)
            outcome.response = HttpResponse(response.status, tuple(response.headers), bytes(body))
        except BaseException as failure:
            # The owner observes this task separately, so cancellation cannot
            # discard either the primary cause or ownership of unfinished I/O.
            outcome.primary = failure
        finally:
            if response is not None:
                try:
                    await response.aclose()
                except BaseException as failure:
                    outcome.cleanup_failures.append(failure)
            try:
                await network.aclose()
            except BaseException as failure:
                outcome.cleanup_failures.append(failure)
            if client is not None:
                try:
                    await client.aclose()
                except BaseException as failure:
                    outcome.cleanup_failures.append(failure)
        return outcome

    async def _observe_retirement(self, task):
        """Observe without cancelling cleanup, even if the caller cancels again."""
        deadline = asyncio.get_running_loop().time() + self._retirement_seconds
        cancellation = None
        while not task.done():
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            try:
                await asyncio.wait({task}, timeout=remaining)
            except asyncio.CancelledError as failure:
                cancellation = failure
        return cancellation

    def _completed(self, task):
        if not task.done():
            return None
        # _perform catches cancellation after entry. A cancelled task here
        # therefore never entered its body and acquired no client or response.
        outcome = _Outcome(primary=asyncio.CancelledError()) if task.cancelled() else task.result()
        if not outcome.cleanup_failures and self._active is task:
            self._active = None
        return outcome

    async def exchange(self, request, *, maximum_bytes):
        """Return only complete responses after cleanup; preserve cancellation."""
        if self._active is not None:
            raise ExchangeFailed("Cannot start HTTPS exchange: an earlier exchange is active or its retirement is unconfirmed")
        if type(maximum_bytes) is not int or maximum_bytes < 0:
            raise ValueError("Cannot receive HTTPS response: byte limit must be a non-negative integer")
        if request.url.scheme != "https":
            raise ValueError("Cannot start HTTPS exchange: destination must use HTTPS")
        self.last_failure = None
        task = asyncio.create_task(self._perform(request, maximum_bytes), name="provider-core-https-exchange")
        self._active = task
        primary = None
        cancellation = None
        try:
            done, _ = await asyncio.wait({task}, timeout=self._exchange_seconds)
            if not done:
                primary = TimeoutError("HTTPS exchange exceeded its aggregate deadline")
        except asyncio.CancelledError as failure:
            primary = cancellation = failure
        if not task.done():
            if not task.cancelling():
                task.cancel()
            later_cancellation = await self._observe_retirement(task)
            if later_cancellation is not None:
                cancellation = later_cancellation
        outcome = self._completed(task)
        interruption = None
        if outcome is not None and outcome.primary is not None and not isinstance(outcome.primary, asyncio.CancelledError):
            interruption = primary
            primary = outcome.primary
        if outcome is not None and primary is None:
            primary = outcome.primary
        cleanup_failures = outcome.cleanup_failures if outcome is not None else ()
        if primary is None and outcome is not None and not cleanup_failures:
            return outcome.response
        if outcome is None or cleanup_failures:
            message = "Cannot complete HTTPS exchange: resource retirement is unconfirmed; this transport cannot accept more work"
        elif isinstance(primary, TimeoutError):
            message = "Cannot complete HTTPS exchange: the aggregate deadline expired; resources were retired"
        elif isinstance(primary, ExchangeFailed):
            message = str(primary)
        else:
            message = "Cannot complete HTTPS exchange: request or response I/O failed; resources were retired"
        if isinstance(interruption, TimeoutError):
            message += "; the aggregate deadline also expired"
        failure = ExchangeFailed(message, primary=primary, cleanup_failures=cleanup_failures, interruption=interruption)
        self.last_failure = failure
        if cancellation is not None or isinstance(primary, asyncio.CancelledError):
            raise cancellation if cancellation is not None else primary
        if isinstance(primary, (KeyboardInterrupt, SystemExit)):
            raise primary
        raise failure from None

    async def retire(self):
        """Stop outstanding I/O and confirm closure; retain failed obligations."""
        task = self._active
        if task is None:
            return
        if not task.done() and not task.cancelling():
            task.cancel()
        cancellation = await self._observe_retirement(task)
        outcome = self._completed(task)
        if self.last_failure is not None and outcome is not None:
            # Late retirement augments the retained exchange evidence; a pending
            # task or its expected cancellation must not erase the original cause.
            if (
                outcome.primary is not None
                and not isinstance(outcome.primary, asyncio.CancelledError)
                and self.last_failure.primary is not outcome.primary
            ):
                self.last_failure.interruption = self.last_failure.primary
                self.last_failure.primary = outcome.primary
            self.last_failure.cleanup_failures = tuple(outcome.cleanup_failures)
        if outcome is None or outcome.cleanup_failures:
            if self.last_failure is None:
                self.last_failure = ExchangeFailed(
                    "Cannot retire HTTPS transport: resource closure remains unconfirmed; retire the enclosing process before replacement",
                    primary=outcome.primary if outcome is not None else None,
                    cleanup_failures=outcome.cleanup_failures if outcome is not None else (),
                )
            if cancellation is None:
                raise self.last_failure from None
        if cancellation is not None:
            raise cancellation
