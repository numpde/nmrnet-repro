"""Choose supplied or generated coordinates in one Attempt-owned conversation.

JobUploads owns membership, grants and byte verification (provider-core/uploads.py).
The existing reader owns parsing and source correspondence; ResultPlan owns final
scientific admission. A chooser receives these concrete operations, never source
URLs, credentials, file paths or a constructor for arbitrary prepared arrays.
The chooser selects Upload records or supplies coordinate/SMILES text for a
verified Job. This owner supplies no external interpreter or format/unit inference.
"""

from tools.container_guard import require_container

require_container()

import asyncio
import hashlib
from builtins import BaseExceptionGroup
from contextlib import asynccontextmanager
from dataclasses import replace
from enum import Enum, auto

from nmrnet_provider.geometry_protocol import require_read_options
from nmrnet_provider.input_geometry import InputRejected, UploadMemberSource, UploadSource
from nmrnet_provider.native_session import NativeWorkerExited
from nmrnet_provider.prepared_input import (
    DEFAULT_OUTPUT,
    GeneratedJobInput,
    OutputSettings,
    PreparedJobInput,
)
from nmrnet_provider.smiles_protocol import require_generation_deadline
from nmrnet_provider.submission import select_job_pasted, select_job_smiles
from nmrnet_provider.worker_protocol import require_seconds
from provider_core.conversation import independent_interruption_failure
from provider_core.job_input import VerifiedJobInput


class InputWorkerCallLimit(RuntimeError):
    """This Job used its configured read/generate calls; no further call began."""

    def __init__(self, *, maximum_calls, calls_started):
        super().__init__(
            f"This Job used its limit of {maximum_calls} attempts to read or generate molecular coordinates, "
            "without selecting usable input. Check the molecular input or ask the provider operator about this limit."
        )
        self.maximum_calls, self.calls_started = maximum_calls, calls_started


class InputSelectionTimeLimit(RuntimeError):
    """This Job's own selection deadline elapsed and original resources retired."""

    def __init__(self, seconds, *, cause):
        super().__init__(
            f"This Job exceeded its {seconds:g}-second limit for selecting molecular input. "
            "Check the request instructions or ask the provider operator about this limit."
        )
        self.seconds, self.cause = seconds, cause


class PreparationRetirementFailed(RuntimeError):
    """Safe caller text with ordered private cleanup causes."""

    def __init__(self, failures, *, operation_failure=None):
        super().__init__("Input resource retirement is unconfirmed; preserve its allocations and private cleanup evidence.")
        self.failures = tuple(failures)
        self.operation_failure = operation_failure
        self.cause = operation_failure if operation_failure is not None else self.failures[0][1]
        self.interruption_cause = None


class _Phase(Enum):
    NEW = auto()
    CHOOSING = auto()
    SELECTED = auto()
    FINISHED = auto()
    RETIRING = auto()
    RETIRED = auto()


def _selected_output(value):
    if type(value) is OutputSettings:
        return value
    try:
        return OutputSettings.from_json(value)
    except ValueError as cause:
        raise InputRejected(str(cause)) from cause


def require_preparation_settings(*, generation_settings, generation_seconds, max_worker_calls, seconds):
    """Admit trusted budgets before startup acquires source or worker resources."""
    if type(max_worker_calls) is not int or max_worker_calls <= 0:
        raise ValueError("Cannot configure input preparation: the worker call limit must be a positive integer.")
    require_seconds(seconds)
    require_generation_deadline(generation_settings, generation_seconds)


class InputPreparation:
    """One conversation, source cache and active reader or generator allocation.

    Composition grants dedicated source API/store transports, separate from
    Attempt polling. Each worker factory owns partial allocation until it returns
    its client/host-retirement pair. The retained allocation task is shielded:
    cancellation cannot discard an allocation that completes after the caller.
    Core acquisition limits count downloads. max_worker_calls separately counts
    allocation/operation attempts, including failed allocations and worker input
    rejections. Local invalid selections use no worker call.
    Neither budget resets during corrections; total time includes chooser waits.
    The chooser grants run/retire operations. run may return immutable secret-free
    observation JSON; None means no recorded external interpretation. Its I/O must
    retire before a candidate leaves this owner, even after preparation succeeds.
    """

    def __init__(
        self,
        *,
        uploads,
        source_api_transport,
        source_store_transport,
        allocate_reader,
        allocate_generator,
        generation_settings,
        generation_seconds,
        chooser,
        max_worker_calls,
        seconds,
    ):
        require_preparation_settings(
            generation_settings=generation_settings,
            generation_seconds=generation_seconds,
            max_worker_calls=max_worker_calls,
            seconds=seconds,
        )
        self._uploads = uploads
        self._transports = (("source_api_retirement", source_api_transport), ("source_store_retirement", source_store_transport))
        self._allocate_reader = allocate_reader
        self._allocate_generator = allocate_generator
        self._generation_settings, self._generation_seconds = generation_settings, generation_seconds
        self._chooser = chooser
        self._chooser_task = None
        self._chooser_retirement_task = None
        self._max_worker_calls, self._seconds = max_worker_calls, seconds
        self._worker_calls = 0
        self._phase = _Phase.NEW
        self._busy = False
        self._source = None
        self._source_task = None
        self._worker_task = None
        self._instructions = None
        self._candidate = None
        self.last_retirement_failure = None
        self.allocation_failure = None
        self.source_failure = None
        self.chooser_failure = None
        self._interpreter_diagnostics = None

    @property
    def interpreter_diagnostics(self):
        """Private interpreter observations, separate from public interpretation."""
        return self._interpreter_diagnostics

    def retain_interpreter_diagnostics(self, content):
        """Retain immutable reviewed evidence before effects and after model cleanup.

        The session admits this with all other evidence under core's existing
        total bound. This capability does not select input or authorize a retry.
        """
        # A cancelled outer waiter can finish before its shielded model cleanup.
        # Late evidence may still arrive; this grants no further input effects.
        if self._phase not in (_Phase.CHOOSING, _Phase.SELECTED, _Phase.FINISHED, _Phase.RETIRING):
            raise ValueError("Interpreter diagnostics require this input conversation or its resource retirement.")
        if type(content) is not bytes:
            raise TypeError("Interpreter diagnostics must be immutable JSON bytes.")
        self._interpreter_diagnostics = content

    async def prepare(self, instructions):
        if self._phase is not _Phase.NEW:
            raise ValueError("Input preparation already started or retired; create a new Attempt-owned preparation session.")
        if type(instructions) is not VerifiedJobInput or instructions.job_ref != self._uploads.job_ref:
            raise ValueError("Input preparation requires the verified instructions for its Job's Uploads.")
        self._phase = _Phase.CHOOSING
        self._instructions = instructions
        selection_deadline = asyncio.timeout(self._seconds)
        try:
            async with self._conversation():
                async with selection_deadline:
                    self._chooser_task = asyncio.create_task(self._run_chooser(instructions))
                    self._chooser_task.add_done_callback(self._observe_chooser)
                    cancellations = asyncio.current_task().cancelling()
                    try:
                        observation = await asyncio.shield(self._chooser_task)
                    except asyncio.CancelledError:
                        # shield replaces a child cancellation, including its
                        # reader/cleanup cause chain. Restore that observed child
                        # failure unless this caller itself was interrupted.
                        if asyncio.current_task().cancelling() == cancellations and isinstance(
                            self.chooser_failure, asyncio.CancelledError
                        ):
                            raise self.chooser_failure from self.chooser_failure.__cause__
                        raise
                if self._phase is not _Phase.SELECTED:
                    raise ValueError("Input preparation did not select valid input before finishing or retirement.")
                if observation is not None:
                    if type(observation) is not bytes:
                        raise TypeError("Interpreter observations must be immutable JSON bytes or None.")
                    self._candidate = replace(self._candidate, interpretation=observation)
            if self._phase is not _Phase.SELECTED:
                raise ValueError("Input preparation retired while observing model cleanup; its candidate cannot be accepted.")
            return self._candidate
        except TimeoutError as failure:
            if not selection_deadline.expired():
                raise
            # Only this timer establishes a Job limit. Settle original source/
            # native work before inspecting failures that may arrive after it.
            # Exceptions from retirement itself remain operational.
            await self.retire()
            independent = [
                observed
                for observed in (
                    self.source_failure,
                    self.allocation_failure,
                    self.last_retirement_failure,
                )
                if observed is not None and not isinstance(observed, asyncio.CancelledError)
            ]
            chooser_failure = independent_interruption_failure(self.chooser_failure)
            if chooser_failure is not None:
                independent.append(chooser_failure)
            # Source cancellation can retain a grant/deadline/cleanup failure;
            # that owner's evidence is not a bare cancelled selection wait.
            if isinstance(self.source_failure, asyncio.CancelledError) and self.source_failure.__cause__ is not None:
                independent.append(self.source_failure.__cause__)
            if independent:
                raise BaseExceptionGroup(
                    "Molecular input deadline expired with independent operation failures", (failure, *independent)
                ) from None
            raise InputSelectionTimeLimit(self._seconds, cause=failure) from None
        finally:
            if self._phase in (_Phase.CHOOSING, _Phase.SELECTED):
                self._phase = _Phase.FINISHED

    @asynccontextmanager
    async def _conversation(self):
        """Preserve the operation failure if observing model-I/O cleanup also fails."""
        operation_failure = None
        try:
            yield
        except BaseException as cause:
            operation_failure = cause
            raise
        finally:
            try:
                if type(operation_failure) is not NativeWorkerExited:
                    try:
                        await self._retire_chooser()
                    except PreparationRetirementFailed as cleanup_failure:
                        self._raise_retirement_failure(cleanup_failure.failures, operation_failure=operation_failure)
                    except (Exception, asyncio.CancelledError) as cleanup_failure:
                        self._raise_retirement_failure(
                            (("interpreter_retirement", cleanup_failure),), operation_failure=operation_failure
                        )
            finally:
                self._retain_chooser_interruption(operation_failure)

    def _retain_chooser_interruption(self, operation_failure):
        """A successful child join must preserve operation evidence without becoming a cleanup failure."""
        child = self.chooser_failure
        if not (operation_failure is not None and child is not None and child is not operation_failure):
            return
        if isinstance(child, asyncio.CancelledError) and child.__cause__ is None:
            return
        previous = operation_failure.__cause__
        if previous is not child:
            operation_failure.__cause__ = (
                child if previous is None else BaseExceptionGroup("Input interruption observations", (previous, child))
            )

    async def _retire_chooser(self):
        if self._chooser_retirement_task is None:
            self._chooser_retirement_task = asyncio.create_task(self._close_conversation())
        await asyncio.shield(self._chooser_retirement_task)

    async def _close_conversation(self):
        """One shared stop/join/close observation, including concurrent retirement."""
        failures = []
        if self._chooser_task is not None:
            try:
                await self._settle_task(self._chooser_task)
            except (Exception, asyncio.CancelledError) as cause:
                failures.append(("interpreter_conversation", cause))
        try:
            async with asyncio.timeout(self._seconds):
                await self._chooser.retire()
        except (Exception, asyncio.CancelledError) as cause:
            failures.append(("interpreter_retirement", cause))
        if failures:
            # Keep cancellation as data inside this shared task. Each observing
            # caller must retain its exact cancellation and all cleanup causes.
            raise PreparationRetirementFailed(failures)

    async def _run_chooser(self, instructions):
        try:
            return await self._chooser.run(instructions, self)
        except BaseException as cause:
            self.chooser_failure = cause
            raise

    def _observe_chooser(self, task):
        # Joining can time out before the chooser fails. Retrieve late private
        # exceptions without replacing the exact cancellation retained by run.
        if not task.cancelled():
            task.exception()

    def _require_choosing(self):
        if self._phase is not _Phase.CHOOSING:
            raise ValueError("Input choice is not active: it has not started, already selected input or is retiring.")

    @asynccontextmanager
    async def _operation(self):
        self._require_choosing()
        if self._busy:
            raise ValueError("Cannot choose input while another source operation is active.")
        if self._worker_task is not None:
            raise RuntimeError("Cannot choose input: the previous worker's retirement is unconfirmed.")
        if self._source_task is not None:
            raise RuntimeError(
                "Cannot choose input: the previous source operation has not settled; retire the preparation session."
            )
        self._busy = True
        try:
            yield
        finally:
            self._busy = False

    async def inspect(self):
        """Expose the actual Upload membership observation, including descriptions."""
        async with self._operation():
            return await self._source_call(self._uploads.inspect)

    async def inspect_upload(self, upload_ref):
        """Inspect cached verified bytes with one separately retired reader allocation.

        Inspection consumes the existing worker-call budget even though it
        decompresses no member. Every read has a 1 MiB member ceiling plus one
        overflow probe, so max_worker_calls also bounds aggregate expansion across
        corrections. Authenticated acquisition byte/request counters stay unchanged.
        """
        async with self._operation():
            self._require_worker_budget()
            source = await self._upload_source(upload_ref)
            async with self._worker(self._allocate_reader) as reader:
                inventory = await reader.inspect_upload(source.data)
            self._require_choosing()
            return {
                "job_ref": source.job_ref,
                "upload_ref": source.upload_ref,
                "byte_length": source.byte_length,
                "content_hash": source.content_hash,
                **inventory,
            }

    async def _upload_source(self, upload_ref):
        if self._source is None or self._source.upload_ref != upload_ref:
            self._source = await self._source_call(self._uploads.acquire, upload_ref)
        return self._source

    async def _source_call(self, operation, *args):
        self._source_task = asyncio.create_task(operation(*args))
        self._source_task.add_done_callback(self._record_source_failure)
        try:
            result = await asyncio.shield(self._source_task)
            self._require_choosing()
            return result
        finally:
            if self._source_task is not None and self._source_task.done():
                self._source_task = None

    def _record_source_failure(self, task):
        try:
            self.source_failure = task.exception()
        except asyncio.CancelledError as cause:
            # shield can replace a child's cancellation. Retain its original
            # owned cause without leaking it to the event loop's default logger.
            self.source_failure = cause

    # silent-defaults: allow-contract -- Omitted member selects a direct file; ZIP requires a name. JSON owns its unit; other formats require one. Output uses the shared settings; malformed options reject before effects.
    async def prepare_upload(
        self,
        upload_ref,
        *,
        source_format,
        source_unit=None,
        selection,
        member=None,
        alternate_location=None,
        output=DEFAULT_OUTPUT,
    ):
        """Select admitted coordinates only after reader retirement is confirmed."""
        async with self._operation():
            self._candidate = None
            selected_output = _selected_output(output)
            require_read_options(source_format, source_unit, selection, alternate_location)
            if member is not None and (type(member) is not str or not member):
                raise InputRejected(
                    "Cannot select ZIP content: member must be an exact nonempty name, or null for a direct file."
                )
            self._require_worker_budget()
            source = await self._upload_source(upload_ref)
            selected_source = UploadSource(
                source.job_ref, source.upload_ref, source.byte_length, source.content_hash.removeprefix("sha256:")
            )
            async with self._worker(self._allocate_reader) as reader:
                options = {
                    "source_format": source_format,
                    "source_unit": source_unit,
                    "selection": selection,
                    "alternate_location": alternate_location,
                }
                if member is None:
                    geometries = await reader.read(source.data, **options)
                else:
                    prepared = await reader.read_member(source.data, member=member, **options)
                    geometries = prepared.geometries
                    selected_source = UploadMemberSource(
                        selected_source, member, len(prepared.source_bytes), hashlib.sha256(prepared.source_bytes).hexdigest()
                    )
            self._require_choosing()
            method = source_format
            if source_format == "json":
                method = "direct_coordinate_json"
            elif source_format == "table":
                method = "coordinate_table"
            self._candidate = PreparedJobInput(
                geometries,
                method,
                selected_source,
                output=selected_output,
            )
            self._phase = _Phase.SELECTED

    # silent-defaults: allow-contract -- Omitted output selects the shared immutable defaults; malformed explicit fragments reject before effects.
    async def prepare_pasted(self, *, source_text, source_format, source_unit, output=DEFAULT_OUTPUT):
        """Select one received geometry; line positions refer to tool text."""
        async with self._operation():
            self._candidate = None
            selected_output = _selected_output(output)
            selected = select_job_pasted(
                self._instructions,
                source_text=source_text,
                source_format=source_format,
                source_unit=source_unit,
            )
            self._require_worker_budget()
            selection = None
            if source_format == "xyz":
                selection = "all"
            async with self._worker(self._allocate_reader) as reader:
                geometries = await reader.read(
                    selected.source_bytes,
                    source_format=source_format,
                    source_unit=source_unit,
                    selection=selection,
                )
            self._require_choosing()
            # Read every XYZ block, then reject multiplicity. Selecting frame 1
            # would silently discard later coordinates or malformed atom rows.
            if len(geometries) != 1:
                raise InputRejected(
                    f"Cannot use pasted coordinates: received {len(geometries)} geometries; provide exactly one. "
                    "use an Upload with instructions for multiple geometries."
                )
            method = {"table": "coordinate_table", "xyz": "xyz"}[source_format]
            self._candidate = PreparedJobInput(geometries, method, selected, output=selected_output)
            self._phase = _Phase.SELECTED

    # silent-defaults: allow-contract -- Omitted output selects the shared immutable defaults; malformed explicit fragments reject before effects.
    async def prepare_smiles(self, *, smiles, requested_count, output=DEFAULT_OUTPUT):
        """Select received SMILES after generation and confirmed retirement."""
        async with self._operation():
            self._candidate = None
            selected_output = _selected_output(output)
            selected = select_job_smiles(self._instructions, smiles=smiles, requested_count=requested_count)
            self._require_worker_budget()
            async with self._worker(
                self._allocate_generator, settings=self._generation_settings, operation_seconds=self._generation_seconds
            ) as generator:
                generated = await generator.generate(selected.source_bytes, selected.requested_count)
            self._require_choosing()
            self._candidate = GeneratedJobInput(selected, generated, output=selected_output)
            self._phase = _Phase.SELECTED

    def _require_worker_budget(self):
        if self._worker_calls >= self._max_worker_calls:
            raise InputWorkerCallLimit(maximum_calls=self._max_worker_calls, calls_started=self._worker_calls)

    @asynccontextmanager
    async def _worker(self, allocator, **configuration):
        """Own one allocation through its operation and confirmed retirement."""
        self._require_choosing()
        self._worker_calls += 1
        self._worker_task = asyncio.create_task(allocator(**configuration))
        self._worker_task.add_done_callback(self._record_allocation_failure)
        try:
            client, _ = await asyncio.shield(self._worker_task)
            self._require_choosing()
            yield client
        except NativeWorkerExited:
            # This allocation task still owns the original grant. Session.retire
            # performs cleanup after core can admit the proved crash.
            raise
        except BaseException as operation_failure:
            # Even cancellation must observe the retained allocation. Cleanup
            # uncertainty remains operational and keeps the original cause.
            try:
                await self._retire_worker()
            except PreparationRetirementFailed as cleanup_failure:
                self._raise_retirement_failure(cleanup_failure.failures, operation_failure=operation_failure)
            except (Exception, asyncio.CancelledError) as cleanup_failure:
                self._raise_retirement_failure(
                    (("input_worker_retirement", cleanup_failure),), operation_failure=operation_failure
                )
            raise
        else:
            await self._retire_worker()

    def _record_allocation_failure(self, task):
        if not task.cancelled():
            self.allocation_failure = task.exception()

    async def _retire_worker(self):
        if self._worker_task is None:
            return
        try:
            async with asyncio.timeout(self._seconds):
                client, host = await asyncio.shield(self._worker_task)
        except NativeWorkerExited as failure:
            await failure.native_operation.retire()
            self._worker_task = None
            return
        failures = await host.retire_channel(client)
        if failures:
            self._raise_retirement_failure(failures)
        self._worker_task = None

    async def _settle_source(self):
        """Stop the whole acquisition before closing its reusable HTTP transports."""
        task = self._source_task
        if task is None:
            return
        await self._settle_task(task)
        self._source_task = None

    async def _settle_task(self, task):
        """Cancel/join owned source or chooser work; its callback retains failures."""
        cancellation_count = asyncio.current_task().cancelling()
        if not task.done():
            task.cancel()
        try:
            async with asyncio.timeout(self._seconds):
                await asyncio.shield(task)
        except asyncio.CancelledError:
            if not task.cancelled() or asyncio.current_task().cancelling() > cancellation_count:
                raise
        except Exception:
            # A settled operation failure stays on its owner's failure field.
            # Its I/O owners still have to confirm independent resource cleanup.
            if not task.done():
                raise

    def _raise_retirement_failure(self, failures, *, operation_failure=None):
        self.last_retirement_failure = PreparationRetirementFailed(failures, operation_failure=operation_failure)
        interruption = None
        if isinstance(operation_failure, asyncio.CancelledError):
            interruption = operation_failure
        else:
            for _, cause in failures:
                if isinstance(cause, asyncio.CancelledError):
                    interruption = cause
                    break
        if interruption is not None:
            # Raising the same cancellation replaces its cause. Retain the old
            # cause before attaching this later independent cleanup failure.
            self.last_retirement_failure.interruption_cause = interruption.__cause__
            raise interruption from self.last_retirement_failure
        raise self.last_retirement_failure from None

    async def retire(self):
        """Retire every granted owner; keep failed obligations for later observation."""
        if self._phase is _Phase.RETIRED:
            return
        self._phase = _Phase.RETIRING
        previous = self._chooser_retirement_task
        if previous is not None and previous.done() and not previous.cancelled() and previous.exception() is not None:
            # Explicit retirement may re-observe a completed failed cleanup.
            # Pending cleanup stays shared; choice and scientific work stay shut.
            self._chooser_retirement_task = None
        failures = []
        try:
            await self._retire_chooser()
        except PreparationRetirementFailed as cause:
            failures.extend(cause.failures)
        except (Exception, asyncio.CancelledError) as cause:
            failures.append(("interpreter_retirement", cause))
        try:
            await self._settle_source()
        except (Exception, asyncio.CancelledError) as cause:
            failures.append(("source_operation", cause))
        try:
            await self._retire_worker()
        except (Exception, asyncio.CancelledError) as cause:
            failures.append(("input_worker_retirement", cause))
        for label, transport in self._transports:
            try:
                await transport.retire()
            except (Exception, asyncio.CancelledError) as cause:
                failures.append((label, cause))
        if failures:
            self._raise_retirement_failure(failures)
        self._source = self._candidate = self._instructions = None
        self._phase = _Phase.RETIRED
