"""A fresh contained store and wire replies for the finite runtime controller."""

import asyncio
import threading
from pathlib import Path

from provider_core.attempt_record import MAX_RECORD_BYTES
from provider_core.attempt_store import AttemptStore, StoreLimits
from tests.fakes.api import reply
from tests.fakes.attempt_store import scope
from tests.fakes.attempts import complete_response, observation, progress_response, start_response
from tests.fakes.execution import Session
from tests.fakes.job_input import response


async def empty_store(directory, *, maximum_records):
    store = AttemptStore(Path(directory), scope=scope(),
                         limits=StoreLimits(maximum_records, maximum_records * MAX_RECORD_BYTES, 0, confirmed_history_records=64), operation_seconds=1)
    await store.open()
    return store


def lifecycle_replies():
    return (reply(start_response()), reply(observation()), reply(response()), reply(progress_response()),
            reply(observation()), reply(progress_response(phase="running")), reply(observation()),
            reply(observation()), reply(complete_response(result_schema_id="result.v1")))


class SingleRetirementSession(Session):
    """A successful retirement is final, rather than an idempotent operation."""

    async def retire(self):
        if any(event[0] == "retire" for event in self.events):
            raise RuntimeError("Scripted session was already retired; do not release its resources twice")
        await super().retire()


class StoreCallBarrier:
    """Hold one real store-thread call while retaining its actual async owner."""

    def __init__(self, function):
        self.function = function
        self.reached = threading.Event()
        self.release = threading.Event()

    def call(self):
        self.reached.set()
        if not self.release.wait(3):
            raise TimeoutError("Test store call was not released")
        return self.function()


async def cancel_task(task):
    """Bound cleanup of the watchdog task even after an assertion fails."""
    if not task.done():
        task.cancel()
        await asyncio.wait((task,), timeout=2)
    if not task.done():
        raise TimeoutError("Test task did not settle after cancellation")
    if not task.cancelled():
        task.exception()
