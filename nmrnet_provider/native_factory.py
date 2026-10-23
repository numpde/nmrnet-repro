"""Acquire native ports for the existing configured Attempt assembly.

The admitted persistent runners own fixed profiles and child retirement.
This factory owns each operation/channel and interrupted setup read until returning
the controller or source pair. Ordering follows runtime.py's ownership contract
and service.py's post-admission handoff; no engine or second lifecycle lives here.
"""

from tools.container_guard import require_container

require_container()

import asyncio
from builtins import BaseExceptionGroup
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from nmrnet_provider.native_session import NativeOperation
from nmrnet_provider.geometry_client import GeometryClient
from nmrnet_provider.retirement import retire_worker_channel
from nmrnet_provider.runtime import assemble_attempt
from nmrnet_provider.smiles_client import SmilesClient
from nmrnet_provider.worker_client import PredictionClient
from nmrnet_provider.worker_protocol import require_seconds
from provider_core.controller import StartRecovery, TerminalRecovery
from provider_core.execution import ConditionRecovery, ObservationRecovery


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


class NativeAttemptFactory:
    """Retain partial setup; successfully returned owners inherit their ports."""

    def __init__(
        self,
        *,
        config,
        api,
        store,
        claims,
        execution_identity,
        selected_artifacts,
        policy,
        observation_recovery,
        condition_recovery,
        on_unconfirmed_phase_progress,
        on_prediction_retry,
        on_native_notice,
        start_recovery,
        terminal_recovery,
    ):
        if type(policy) is not NativePolicy:
            raise ValueError("Native factory requires explicit admitted allocation and channel budgets.")
        # Production requires recovery before any allocation. Runtime assembly
        # passes this same policy through; core owns its public-entry validation.
        if type(observation_recovery) is not ObservationRecovery:
            raise ValueError("Native factory requires bounded observation recovery and protected diagnostic observers.")
        if type(condition_recovery) is not ConditionRecovery:
            raise ValueError("Native factory requires bounded condition recovery and protected diagnostic observers.")
        if type(start_recovery) is not StartRecovery:
            raise ValueError("Native factory requires bounded exact START recovery and protected diagnostic observers.")
        if type(terminal_recovery) is not TerminalRecovery:
            raise ValueError("Native factory requires bounded exact terminal recovery and protected diagnostic observers.")
        if not callable(on_unconfirmed_phase_progress):
            raise ValueError("Native factory requires a protected unconfirmed phase-progress observer before allocation.")
        if not callable(on_prediction_retry):
            raise ValueError("Native factory requires a protected prediction-retry observer before allocation.")
        self.config, self.api, self.store, self.claims = config, api, store, claims
        self.execution_identity, self.selected_artifacts, self.policy = execution_identity, selected_artifacts, policy
        self.observation_recovery = observation_recovery
        self.condition_recovery = condition_recovery
        self.on_unconfirmed_phase_progress = on_unconfirmed_phase_progress
        self.on_prediction_retry = on_prediction_retry
        if not callable(on_native_notice):
            raise ValueError("Native factory requires a protected native lifecycle diagnostic observer.")
        self.on_native_notice = on_native_notice
        self.start_recovery = start_recovery
        self.terminal_recovery = terminal_recovery
        self.allocations, self.clients = [], {}
        self.last_failure = None
        self._operation = None

    async def __call__(self, pending):
        self._begin_setup()
        allocation = None
        try:
            worker, allocation = await self._open_role("prediction", job_ref=pending.input.job_ref)
            runtime = allocation.runtime
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
                runtime_expectations=runtime,
                allocate_prediction=partial(self.allocate_prediction, job_ref=pending.input.job_ref),
                on_prediction_retry=self.on_prediction_retry,
                allocate_reader=self.allocate_reader,
                allocate_generator=self.allocate_generator,
                observation_recovery=self.observation_recovery,
                condition_recovery=self.condition_recovery,
                on_unconfirmed_phase_progress=self.on_unconfirmed_phase_progress,
                start_recovery=self.start_recovery,
                terminal_recovery=self.terminal_recovery,
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

    async def allocate_prediction(self, *, job_ref):
        """Transfer one fresh child from the same admitted prediction runner."""
        self._begin_setup()
        allocation = None
        try:
            client, allocation = await self._open_role("prediction", job_ref=job_ref)
            runtime = allocation.runtime
            self._release(allocation)
            return client, allocation.retirement, runtime
        except (Exception, asyncio.CancelledError, BaseExceptionGroup) as cause:
            if allocation is None:
                raise
            failures = await self._retire(allocation)
            if not failures:
                self._release(allocation)
            self._raise_setup(cause, failures, job_ref=job_ref)
        finally:
            self._operation = None

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
        if (
            self._operation is not None
            or self.allocations
            or self.store.pending_owner is self
        ):
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
        allocation = NativeOperation(
            role=role,
            endpoint=Path("/native") / role / "runner.sock",
            operation_seconds=self.policy.allocation_seconds,
            on_notice=lambda failure, facts: self.on_native_notice(failure, {**facts, "job_ref": job_ref}),
        )
        self.allocations.append(allocation)
        writer = None
        try:
            reader, writer = await allocation.start()
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
