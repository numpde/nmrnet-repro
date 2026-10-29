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

from nmrnet_provider.native_session import NativeOperation, NativeWorkerExited
from nmrnet_provider.geometry_client import GeometryClient
from nmrnet_provider.runtime import assemble_attempt
from nmrnet_provider.smiles_client import SmilesClient
from nmrnet_provider.worker_client import PredictionClient
from nmrnet_provider.worker_protocol import require_seconds
from provider_core.controller import LiveStartPolicy
from provider_core.execution import ConditionRecovery, ObservationRecovery


@dataclass(frozen=True, slots=True)
class NativePolicy:
    allocation_seconds: float
    prediction_seconds: float
    source_seconds: float

    def __post_init__(self):
        for seconds in (self.allocation_seconds, self.prediction_seconds, self.source_seconds):
            require_seconds(seconds)

    @property
    def phase_seconds(self):
        """Share the runner's existing allocation budget between its five stages."""
        return self.allocation_seconds / 5


class NativeSetupFailed(RuntimeError):
    """Keep setup causes with the selected Job, when this owner knows it.

    Prediction setup follows START and input preparation. This factory knows
    the Job; its session caller retains the acknowledged Attempt. Source-only
    allocation may have no Job context here; its caller retains the Attempt.
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
        claims,
        admission_stopping,
        execution_identity,
        selected_artifacts,
        policy,
        observation_recovery,
        condition_recovery,
        on_unconfirmed_phase_progress,
        on_operator_notice,
        start_policy,
        on_delivery_retry,
    ):
        if type(policy) is not NativePolicy:
            raise ValueError("Native factory requires explicit admitted allocation and channel budgets.")
        # Production supplies bounded observation and condition handling before
        # allocation. Core validates these same policies at its public boundary.
        if type(observation_recovery) is not ObservationRecovery:
            raise ValueError("Native factory requires bounded observation recovery and protected diagnostic observers.")
        if type(condition_recovery) is not ConditionRecovery:
            raise ValueError("Native factory requires bounded condition recovery and protected diagnostic observers.")
        if type(start_policy) is not LiveStartPolicy:
            raise ValueError("Native factory requires the live START policy.")
        if not callable(on_delivery_retry):
            raise ValueError("Native factory requires a protected delivery retry observer.")
        if not callable(on_unconfirmed_phase_progress):
            raise ValueError("Native factory requires a protected unconfirmed phase-progress observer before allocation.")
        self.config, self.claims = config, claims
        self.admission_stopping = admission_stopping
        self.execution_identity, self.selected_artifacts, self.policy = execution_identity, selected_artifacts, policy
        self.observation_recovery = observation_recovery
        self.condition_recovery = condition_recovery
        self.on_unconfirmed_phase_progress = on_unconfirmed_phase_progress
        if not callable(on_operator_notice):
            raise ValueError("Native factory requires a protected operator diagnostic observer.")
        self.on_operator_notice = on_operator_notice
        self.start_policy = start_policy
        self.on_delivery_retry = on_delivery_retry
        self.allocations, self.clients = [], {}
        self.last_failure = None
        self._operation = None

    @property
    def shutdown_ready(self):
        """Confirm setup owns neither a running invocation nor native grants."""
        return self._operation is None and not self.allocations and not self.clients

    async def __call__(self, pending, *, stopping):
        self._begin_setup()
        try:
            return await assemble_attempt(
                config=self.config,
                pending=pending,
                claims=self.claims,
                stopping=stopping,
                admission_stopping=self.admission_stopping,
                start_policy=self.start_policy,
                execution_identity=self.execution_identity,
                selected_artifacts=self.selected_artifacts,
                allocate_prediction=partial(self.allocate_prediction, job_ref=pending.input.job_ref),
                on_prediction_failure=lambda failure, attempt: self.on_operator_notice(
                    failure, {"event": "prediction_failed", "job_ref": pending.input.job_ref,
                              "execution_attempt_ref": attempt.admission.execution_attempt_ref}
                ),
                on_interpreter_failure=lambda index, failure: self.on_operator_notice(
                    failure, {"event": "interpreter_endpoint_failed", "job_ref": pending.input.job_ref, "endpoint_index": index}
                ),
                on_source_retry=lambda failure, seconds: self.on_operator_notice(
                    failure, {"event": "source_retry", "job_ref": pending.input.job_ref, "retry_seconds": seconds}
                ),
                on_source_observation=lambda failure: self.on_operator_notice(
                    failure, {"event": "source_acquisition_observation", "job_ref": pending.input.job_ref}
                ),
                allocate_reader=self.allocate_reader,
                allocate_generator=self.allocate_generator,
                observation_recovery=self.observation_recovery,
                condition_recovery=self.condition_recovery,
                on_unconfirmed_phase_progress=self.on_unconfirmed_phase_progress,
                on_delivery_retry=self.on_delivery_retry,
            )
        except (Exception, asyncio.CancelledError, BaseExceptionGroup) as cause:
            self._raise_setup(cause, [], job_ref=pending.input.job_ref)
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
            failures = []
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

    def _new_operation(self, role, *, job_ref):
        allocation = NativeOperation(
            role=role,
            endpoint=Path("/native") / role / "runner.sock",
            operation_seconds=self.policy.allocation_seconds,
            observation_seconds=self.policy.phase_seconds,
            on_notice=lambda failure, facts: self.on_operator_notice(failure, {**facts, "job_ref": job_ref}),
        )
        self.allocations.append(allocation)
        return allocation

    async def _open_role(self, role, *, job_ref):
        allocation = self._new_operation(role, job_ref=job_ref)
        writer = None
        try:
            reader, writer = await allocation.start()
            options = {"allocation": allocation.allocation, "operation_seconds": self.policy.source_seconds,
                       "native": allocation}
            if role == "prediction":
                options["operation_seconds"] = self.policy.prediction_seconds
                options["declaration_sha256"] = self.execution_identity["prediction_declaration_sha256"]
                client = PredictionClient(reader, writer, **options)
            elif role == "reader":
                client = GeometryClient(reader, writer, **options)
            else:
                options["operation_seconds"] = self.config.preparation.generation_seconds
                client = SmilesClient(reader, writer, settings=self.config.preparation.generation_settings, **options)
            self.clients[allocation] = client
            return client, allocation
        except NativeWorkerExited:
            # The failed allocation task receives this exact grant. Reporting
            # the proved exit must not wait for inline channel cleanup.
            self._release(allocation)
            raise
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
            failures.extend(await allocation.retire_channel(client))
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
        self.last_failure = failure if self.allocations else None
        if interruption is not None:
            raise interruption from failure
        raise failure from None
