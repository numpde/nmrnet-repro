"""Recording execution and relay ports around the real compact checkpoint owner."""

import asyncio
from pathlib import Path

from provider_core.attempt_record import acknowledge_start
from provider_core.checkpoint_store import CheckpointLimits, CheckpointStore
from provider_core.delivery_client import DeliveryEndpoint
from provider_core.ownership import AttemptClaims
from tests.fakes.attempt_store import admission, record, scope
from tests.fakes.execution import Session


async def acknowledged_store(directory):
    pending = record()
    acknowledged = acknowledge_start(pending, admission())
    store = CheckpointStore(Path(directory), scope=scope(), limits=CheckpointLimits(2, 300000), operation_seconds=1)
    await store.open()
    await store.retain_start(pending)
    await store.acknowledge_start(pending, acknowledged.admission)
    claim = AttemptClaims().claim_start(pending)
    owner = object()
    claim.begin(owner)
    claim.bind_attempt(scope(), acknowledged.admission.execution_attempt_ref, owner=owner)
    claim.finish(owner)
    return store, acknowledged, claim


class CheckpointSession(Session):
    def __init__(self, store):
        super().__init__(store)
        self.retirement_started = asyncio.Event()

    async def prepare(self, acknowledged, instructions, *, report_condition):
        self.events.append(("prepare", (await self.store.inventory())[0], instructions))
        if acknowledged.start.input != instructions.identity:
            raise AssertionError("substituted input")
        return self.preparation

    async def execute(self, prepared, *, report_condition):
        if prepared is not self.handle:
            raise AssertionError("foreign prepared handle")
        self.events.append(("execute", (await self.store.inventory())[0]))
        return self.outcome

    async def retire(self):
        self.retirement_started.set()
        await super().retire()


class RecordingDelivery:
    """Record exact bodies at the receiving boundary, outside producer ownership."""

    def __init__(self):
        self.endpoint = DeliveryEndpoint(scope().api_origin, scope().provider_ref, Path("/tmp/relay.sock"))
        self.items = []
        self.retirement_confirmed = True
        self.last_failure = None
        self.accepted = asyncio.Event()

    async def submit(self, item):
        self.items.append(item)
        self.accepted.set()
        return item.acceptance()

    async def retire(self):
        self.retirement_confirmed = True

    def can_retry(self, failure):
        return False
