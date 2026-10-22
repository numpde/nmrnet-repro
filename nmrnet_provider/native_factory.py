"""Acquire native ports for the existing configured Attempt assembly.

The host owns fixed worker profiles and exact retirement (provider_workers.sh).
This factory owns each request/channel and interrupted setup read until returning
the controller or source pair. Ordering follows runtime.py's ownership contract
and service.py's post-admission handoff; no engine or second lifecycle lives here.
"""

from tools.container_guard import require_container

require_container()

import asyncio
import secrets
from builtins import BaseExceptionGroup
from dataclasses import dataclass
from pathlib import Path

from nmrnet_provider.allocation import WorkerAllocation
from nmrnet_provider.geometry_client import GeometryClient
from nmrnet_provider.retirement import retire_worker_channel
from nmrnet_provider.runtime import assemble_attempt
from nmrnet_provider.smiles_client import SmilesClient
from nmrnet_provider.worker_client import PredictionClient
from nmrnet_provider.worker_protocol import document, require_seconds


@dataclass(frozen=True, slots=True)
class NativePolicy:
    allocation_seconds: float
    prediction_seconds: float
    source_seconds: float

    def __post_init__(self):
        for seconds in (self.allocation_seconds, self.prediction_seconds, self.source_seconds):
            require_seconds(seconds)


class NativeSetupFailed(RuntimeError):
    """Keep setup causes with the selected Job, when this owner knows it.

    Prediction setup precedes START. Source-only allocation may have no Job
    context here; its caller retains the acknowledged Attempt instead.
    """

    def __init__(self, cause, cleanup_failures, *, interruption_cause, job_ref):
        super().__init__(
            "Cannot assemble native NMRNet work. Preserve the factory's original requests and protected causes; "
            "settle setup I/O and confirm exact worker retirement before replacement."
        )
        self.cause, self.cleanup_failures = cause, cleanup_failures
        self.interruption_cause = interruption_cause
        self.job_ref = job_ref


def _observe_close(task):
    if not task.cancelled():
        task.exception()


class NativeAttemptFactory:
    """Retain partial setup; successfully returned owners inherit their ports."""

    def __init__(self, *, config, api, store, claims, execution_identity, selected_artifacts, policy):
        if type(policy) is not NativePolicy:
            raise ValueError("Native factory requires explicit admitted allocation and channel budgets.")
        self.config, self.api, self.store, self.claims = config, api, store, claims
        self.execution_identity, self.selected_artifacts, self.policy = execution_identity, selected_artifacts, policy
        self.allocations, self.clients = [], {}
        self.channel_closures = {}
        self.last_failure = None
        self._operation = None

    async def __call__(self, pending):
        self._begin_setup()
        allocation = None
        try:
            worker, allocation = await self._open_role("prediction", job_ref=pending.input.job_ref)
            metadata = document(allocation.metadata)
            if set(metadata) != {"runtime"} or type(metadata["runtime"]) is not dict:
                raise ValueError("Prediction startup metadata must contain one observed runtime object.")
            controller = await assemble_attempt(
                config=self.config,
                pending=pending,
                api=self.api,
                store=self.store,
                setup_owner=self,
                claims=self.claims,
                worker=worker,
                retirement=allocation.retirement,
                execution_identity=self.execution_identity,
                selected_artifacts=self.selected_artifacts,
                runtime_expectations=metadata["runtime"],
                allocate_reader=self.allocate_reader,
                allocate_generator=self.allocate_generator,
            )
            self._release(allocation)
            return controller
        except (Exception, asyncio.CancelledError, BaseExceptionGroup) as cause:
            if allocation is None:
                # _open_role owns its partial setup and already retained every
                # cleanup cause. In particular, do not wrap its cancellation again.
                raise
            failures = []
            failures.extend(await self._settle_store())
            cleanup = await self._retire(allocation)
            failures.extend(cleanup)
            if not cleanup:
                self._release(allocation)
            self._raise_setup(cause, failures, job_ref=pending.input.job_ref)
        finally:
            self._operation = None

    async def allocate_reader(self):
        return await self._source("reader")

    async def allocate_generator(self, *, settings, operation_seconds):
        if (
            settings != self.config.preparation.generation_settings
            or operation_seconds != self.config.preparation.generation_seconds
        ):
            raise ValueError("Cannot allocate conformer worker: generation settings must match admitted startup configuration.")
        return await self._source("smiles")

    async def _source(self, role):
        self._begin_setup()
        try:
            client, allocation = await self._open_role(role, job_ref=None)
            self._release(allocation)
            return client, allocation.retirement
        finally:
            self._operation = None

    def _begin_setup(self):
        if self._operation is not None or self.allocations or self.store.pending_owner is self:
            raise RuntimeError(
                "Cannot acquire new native work: await active setup and settle this factory's original obligations first."
            )
        self._operation = asyncio.current_task()
        self.last_failure = None

    def _release(self, allocation):
        # No await separates successful handoff from forgetting setup handles.
        # The controller/source owner now retains the original client/retirement.
        self.allocations.remove(allocation)
        if allocation in self.clients:
            del self.clients[allocation]
        if allocation in self.channel_closures:
            del self.channel_closures[allocation]

    async def settle(self):
        """Retry observation/cleanup of held setup only; never allocate or reconnect."""
        if self._operation is not None:
            raise RuntimeError("Cannot settle native setup while its original invocation is active; await that invocation first.")
        self._operation = asyncio.current_task()
        try:
            failures = await self._settle_store()
            for allocation in tuple(self.allocations):
                cleanup = await self._retire(allocation)
                failures.extend(cleanup)
                if not cleanup:
                    self._release(allocation)
            if failures:
                primary = self.last_failure.cause if self.last_failure is not None else failures[0][1]
                job_ref = None
                if self.last_failure is not None:
                    job_ref = self.last_failure.job_ref
                self._raise_setup(primary, failures, job_ref=job_ref)
            self.last_failure = None
        finally:
            self._operation = None

    async def _settle_store(self):
        if self.store.pending_owner is self:
            try:
                await self.store.settle(owner=self)
            except (Exception, asyncio.CancelledError, BaseExceptionGroup) as cause:
                return [("setup storage read", cause)]
        return []

    async def _open_role(self, role, *, job_ref):
        allocation = WorkerAllocation(
            role=role,
            allocation=secrets.token_hex(16),
            request_root=Path("/native/requests"),
            receipt_root=Path("/native/receipts"),
            ipc_root=Path("/native/ipc"),
            operation_seconds=self.policy.allocation_seconds,
        )
        self.allocations.append(allocation)
        writer = None
        try:
            await allocation.start()
            name = "science.sock" if role == "prediction" else "geometry.sock"
            async with asyncio.timeout(self.policy.allocation_seconds):
                reader, writer = await asyncio.open_unix_connection(str(allocation.ipc / name))
            options = {"allocation": allocation.allocation, "operation_seconds": self.policy.source_seconds}
            if role == "prediction":
                options["operation_seconds"] = self.policy.prediction_seconds
                client = PredictionClient(reader, writer, **options)
            elif role == "reader":
                client = GeometryClient(reader, writer, **options)
            else:
                options["operation_seconds"] = self.config.preparation.generation_seconds
                client = SmilesClient(reader, writer, settings=self.config.preparation.generation_settings, **options)
            self.clients[allocation] = client
            return client, allocation
        except (Exception, asyncio.CancelledError, BaseExceptionGroup) as cause:
            failures = []
            if writer is not None and allocation not in self.clients:
                try:
                    writer.close()
                except (Exception, asyncio.CancelledError) as cleanup:
                    failures.append(("unadopted channel close", cleanup))
                try:
                    closure = asyncio.create_task(writer.wait_closed())
                    closure.add_done_callback(_observe_close)
                    self.channel_closures[allocation] = closure
                except Exception as cleanup:
                    failures.append(("unadopted close observation", cleanup))
            failures.extend(await self._retire(allocation))
            if not failures:
                self._release(allocation)
            self._raise_setup(cause, failures, job_ref=job_ref)

    async def _retire(self, allocation):
        failures = []
        if allocation in self.clients:
            client = self.clients[allocation]
            failures.extend(await retire_worker_channel(client, allocation.retirement))
        else:
            try:
                await allocation.retirement.retire()
            except (Exception, asyncio.CancelledError, BaseExceptionGroup) as cause:
                failures.append(("native retirement", cause))
        try:
            await allocation.settle()
        except (Exception, asyncio.CancelledError, BaseExceptionGroup) as cause:
            failures.append(("allocation I/O", cause))
        if allocation in self.channel_closures:
            closure = self.channel_closures[allocation]
            try:
                async with asyncio.timeout(self.policy.allocation_seconds):
                    await asyncio.shield(closure)
            except (Exception, asyncio.CancelledError) as cause:
                failures.append(("unadopted channel observation", cause))
        return failures

    def _raise_setup(self, cause, failures, *, job_ref):
        interruption = None
        if isinstance(cause, asyncio.CancelledError):
            interruption = cause
        else:
            for _, cleanup in failures:
                if isinstance(cleanup, asyncio.CancelledError):
                    interruption = cleanup
                    break
        failure = NativeSetupFailed(
            cause,
            tuple(failures),
            interruption_cause=interruption.__cause__ if interruption is not None else None,
            job_ref=job_ref,
        )
        # Resolved failures belong to the caller receiving this exception. Keep
        # factory diagnostics only while their original cleanup remains held.
        self.last_failure = failure if self.allocations or self.store.pending_owner is self else None
        if interruption is not None:
            raise interruption from failure
        raise failure from None
