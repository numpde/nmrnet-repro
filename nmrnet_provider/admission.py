"""Observe service admission and select fresh work without allocating it.

NMRPeak 1697dc84dd1d76ae25b04efc8183fd80614000e5,
nmrpeak_provider/attempt_inventory.py:62-116 and provider_process.py:192-214,
445-495 establish complete bounded inventory before fresh work. Here any live
remote row refuses admission, so no collected reconciliation map is needed.
API 6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8,
nmr_api/operations/provider_execution_attempt_list_service.py:38-108 owns the
provider-wide visible live view. Empty traversal is neither an atomic snapshot
nor a cross-process fence. Host singleton ownership and server admission remain
authoritative. Core owns page syntax, exact start bodies and durable admission.
"""

from tools.container_guard import require_container

require_container()

import secrets
from dataclasses import dataclass

from nmrnet_provider.config import ANALYSIS_KIND_REF, ProviderConfig
from provider_core.attempt_record import StartPending, TerminalConfirmed
from provider_core.attempt_request import start_body
from provider_core.paging import paging_query


class AdmissionHeld(RuntimeError):
    """Safe hold explanation; remote ownership observations stay protected."""

    def __init__(self, message, *, remote_attempts=()):
        super().__init__(message)
        self.remote_attempts = remote_attempts


@dataclass(frozen=True, slots=True)
class AdmissionPolicy:
    page_size: int
    maximum_pages: int

    def __post_init__(self):
        paging_query(limit=self.page_size, cursor=None)
        if type(self.maximum_pages) is not int or self.maximum_pages < 1:
            raise ValueError("Cannot configure service admission: maximum_pages must be a positive integer.")


def _require_context(config, api, policy, owner):
    if type(config) is not ProviderConfig or type(policy) is not AdmissionPolicy or owner is None:
        raise ValueError(
            "Cannot observe service admission: admitted configuration, page policy and an explicit I/O owner are required."
        )
    if api.identity is not config.api.identity or api.expected_topology != config.api.expected_topology:
        raise ValueError("Cannot observe service admission: API must use the configured signing identity and topology.")


async def require_admission_ready(config, api, store, *, policy, owner):
    """Hold fresh work until local capacity and a complete empty live view agree.

    This reserves nothing and resolves no recovery obligation. With per-exchange
    deadlines supplied by the configured API, maximum_pages bounds the whole
    traversal's work. Failure/cancellation propagates unchanged; the caller must
    settle any original store/API I/O under owner before reuse or closure.
    """
    _require_context(config, api, policy, owner)
    if store.scope != config.lifecycle.scope:
        raise ValueError("Cannot observe service admission: retained storage must use the configured recovery scope.")
    records = await store.admission_inventory(owner=owner)
    if any(type(record) is not TerminalConfirmed for record in records):
        raise AdmissionHeld(
            "Cannot admit fresh NMRNet work: retained Attempt obligations require explicit recovery. Preserve their records."
        )
    cursor, seen = None, set()
    for _page_number in range(policy.maximum_pages):
        page = await api.execution_attempts(state="in_progress", limit=policy.page_size, cursor=cursor, owner=owner)
        if page.attempts:
            raise AdmissionHeld(
                "Cannot admit fresh NMRNet work: the provider has in-progress Attempts. Reconcile ownership before starting more work.",
                remote_attempts=page.attempts,
            )
        cursor = _next_cursor(page.next_cursor, seen, operation="live Attempt inventory")
        if cursor is None:
            return
    raise AdmissionHeld(
        "Cannot admit fresh NMRNet work: live Attempt inventory exceeded the page bound. Admission remains held until a complete view is available."
    )


async def select_pending_start(config, api, *, policy, owner):
    """Select the first eligible server-ordered Job, starting each walk afresh.

    Returns an in-memory start, not a retained command or execution permission.
    The service serializes readiness, selection and allocation; abandonment
    requires fresh observations, not reuse of this selection as a readiness token.
    The existing controller must retain these exact start bytes before sending.
    """
    _require_context(config, api, policy, owner)
    cursor, seen = None, set()
    for _page_number in range(policy.maximum_pages):
        page = await api.jobs(
            analysis_kind_ref=ANALYSIS_KIND_REF,
            has_provider_execution_attempt=False,
            limit=policy.page_size,
            cursor=cursor,
            owner=owner,
        )
        if page.jobs:
            selected = page.jobs[0]
            body = start_body(selected.input.job_ref, secrets.token_hex(16))
            return StartPending(config.lifecycle.scope, selected.analysis_kind_ref, selected.input, body)
        cursor = _next_cursor(page.next_cursor, seen, operation="Job discovery")
        if cursor is None:
            return None
    raise AdmissionHeld(
        "Cannot select fresh NMRNet work: Job discovery exceeded the page bound. Retry only after reviewing the incomplete discovery."
    )


def _next_cursor(cursor, seen, *, operation):
    if cursor is not None:
        if cursor in seen:
            raise AdmissionHeld(
                f"Cannot finish {operation}: the API repeated a page cursor. Preserve the failed observation and investigate before retrying."
            )
        seen.add(cursor)
    return cursor
