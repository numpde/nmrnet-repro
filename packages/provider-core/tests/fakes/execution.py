"""Recording async session: proves core ordering, not actual worker retirement."""

from pathlib import Path

from provider_core.analysis import AnalysisSuccess
from provider_core.attempt_record import MAX_RECORD_BYTES, ProtectedEvidence, acknowledge_start
from provider_core.attempt_store import AttemptStore, StoreLimits
from provider_core.ownership import AttemptClaims
from tests.fakes.attempt_store import admission, record, scope
from tests.fakes.attempts import ATTEMPT_REF


async def acknowledged_store(directory):
    pending = record()
    acknowledged = acknowledge_start(pending, admission())
    store = AttemptStore(Path(directory), scope=scope(), limits=StoreLimits(1, MAX_RECORD_BYTES, 0), operation_seconds=1)
    await store.open()
    await store.admit(pending)
    await store.replace(pending, acknowledged)
    claim = AttemptClaims().claim_start(pending)
    owner = object()
    claim.begin(owner)
    claim.bind_attempt(scope(), ATTEMPT_REF, owner=owner)
    claim.finish(owner)
    return store, acknowledged, claim


class Session:
    def __init__(self, store):
        self.store = store
        self.handle = object()
        self.events = []
        self.preparation = self.handle
        self.outcome = AnalysisSuccess("result.v1", b"result bytes", ProtectedEvidence("evidence.v1", b"private evidence"))
        self.retirement_failure = None

    async def prepare(self, acknowledged, instructions):
        self.events.append(("prepare", (await self.store.records())[0], instructions))
        if acknowledged.start.input != instructions.identity:
            raise AssertionError("substituted input")
        return self.preparation

    async def execute(self, prepared):
        if prepared is not self.handle:
            raise AssertionError("foreign prepared handle")
        self.events.append(("execute", (await self.store.records())[0]))
        return self.outcome

    async def retire(self):
        self.events.append(("retire",))
        if self.retirement_failure is not None:
            raise self.retirement_failure
