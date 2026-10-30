"""Select Jobs without provider Attempts or existing live ownership."""

from tools.container_guard import require_container

require_container()

import secrets
from dataclasses import dataclass

from nmrnet_provider.config import ANALYSIS_KIND_REF, ProviderConfig
from provider_core.attempt_record import StartPending
from provider_core.attempt_request import start_body
from provider_core.paging import paging_query


class AdmissionIncomplete(RuntimeError):
    """The bounded Job feed did not provide a complete usable page sequence."""


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


async def select_pending_start(config, api, *, policy, owner, claims):
    """Choose a never-attempted Job outside current live ownership."""
    _require_context(config, api, policy, owner)
    cursor, seen = None, set()
    for _page_number in range(policy.maximum_pages):
        page = await api.jobs(analysis_kind_ref=ANALYSIS_KIND_REF, has_provider_execution_attempt=False,
                              limit=policy.page_size, cursor=cursor, owner=owner)
        for selected in page.jobs:
            if not claims.owns_job(config.lifecycle.scope, selected.input.job_ref):
                body = start_body(selected.input.job_ref, secrets.token_hex(16))
                return StartPending(config.lifecycle.scope, selected.analysis_kind_ref, selected.input, body)
        cursor = _next_cursor(page.next_cursor, seen, operation="Job discovery")
        if cursor is None:
            return None
    raise AdmissionIncomplete("Job discovery exceeded its page bound; review the incomplete view before retrying.")


def _next_cursor(cursor, seen, *, operation):
    if cursor is not None:
        if cursor in seen:
            raise AdmissionIncomplete(f"Cannot finish {operation}: the API repeated a page cursor; investigate the incomplete observation.")
        seen.add(cursor)
    return cursor
