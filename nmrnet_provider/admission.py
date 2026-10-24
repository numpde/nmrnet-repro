"""Select fresh science after scoped control, relay and live API observations.

NMRPeak 1697dc84dd1d76ae25b04efc8183fd80614000e5,
nmrpeak_provider/attempt_inventory.py:62-116 and provider_process.py:192-214,
445-495 establish complete bounded inventory before fresh work. Note021 moves
terminal ownership to the relay: its current scoped rows exclude those exact
Jobs/Attempts from computation without claiming API publication. These reads
reserve no queue space and never authorize replay of scientific work.
"""

from tools.container_guard import require_container

require_container()

import secrets
from dataclasses import dataclass

from nmrnet_provider.config import ANALYSIS_KIND_REF, ProviderConfig
from provider_core.attempt_record import StartPending
from provider_core.attempt_request import start_body
from provider_core.delivery_store import DeliveryFull
from provider_core.paging import paging_query


class AdmissionHeld(RuntimeError):
    """Safe hold explanation; remote ownership observations stay protected."""

    # silent-defaults: allow-contract -- Local control holds have no remote inventory; remote ownership holds retain the actual received rows.
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
        raise ValueError("Admission requires configured API identity, page policy and an explicit I/O owner.")
    if api.identity is not config.api.identity or api.expected_topology != config.api.expected_topology:
        raise ValueError("Admission API must use the configured signing identity and topology.")


async def require_admission_ready(config, api, store, *, delivery, policy, owner):
    """Return current relay inventory after excluding only its exact remote rows."""
    _require_context(config, api, policy, owner)
    if store.scope != config.lifecycle.scope:
        raise ValueError("Admission checkpoint storage must use the configured recovery scope.")
    if await store.inventory(owner=owner):
        raise AdmissionHeld("Retained producer control requires reconciliation before new science; no previous computation will be repeated.")
    relay = await delivery.pending()
    owned = {(item["execution_attempt_ref"], item["job_ref"]) for item in relay["items"]}
    if (len(relay["items"]) >= relay["capacity"]["provider_items"]
            or sum(item["body_bytes"] for item in relay["items"]) >= relay["capacity"]["provider_bytes"]):
        raise DeliveryFull("Delivery capacity is full; wait for existing results before starting more computation.")
    cursor, seen = None, set()
    for _page_number in range(policy.maximum_pages):
        page = await api.execution_attempts(state="in_progress", limit=policy.page_size, cursor=cursor, owner=owner)
        unowned = tuple(item for item in page.attempts if (item.execution_attempt_ref, item.job_ref) not in owned)
        if unowned:
            raise AdmissionHeld("The provider has in-progress Attempts without verified relay ownership; reconcile them before new science.", remote_attempts=unowned)
        cursor = _next_cursor(page.next_cursor, seen, operation="live Attempt inventory")
        if cursor is None:
            return relay
    raise AdmissionHeld("Live Attempt inventory exceeded its page bound; admission needs a complete view.")


async def select_pending_start(config, api, *, policy, owner, excluded_jobs):
    """Choose the first eligible Job outside this cycle's actual relay inventory."""
    _require_context(config, api, policy, owner)
    cursor, seen = None, set()
    for _page_number in range(policy.maximum_pages):
        page = await api.jobs(analysis_kind_ref=ANALYSIS_KIND_REF, has_provider_execution_attempt=False,
                              limit=policy.page_size, cursor=cursor, owner=owner)
        for selected in page.jobs:
            if selected.input.job_ref not in excluded_jobs:
                body = start_body(selected.input.job_ref, secrets.token_hex(16))
                return StartPending(config.lifecycle.scope, selected.analysis_kind_ref, selected.input, body)
        cursor = _next_cursor(page.next_cursor, seen, operation="Job discovery")
        if cursor is None:
            return None
    raise AdmissionHeld("Job discovery exceeded its page bound; review the incomplete view before retrying.")


def _next_cursor(cursor, seen, *, operation):
    if cursor is not None:
        if cursor in seen:
            raise AdmissionHeld(f"Cannot finish {operation}: the API repeated a page cursor; investigate the incomplete observation.")
        seen.add(cursor)
    return cursor
