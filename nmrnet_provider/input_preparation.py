"""Choose supplied or generated coordinates in one Attempt-owned conversation.

JobUploads owns membership, grants and byte verification (provider-core/uploads.py).
The existing reader owns parsing and source correspondence; ResultPlan owns final
scientific admission. A chooser receives these concrete operations, never source
URLs, credentials, file paths or a constructor for arbitrary prepared arrays.
The chooser may select Upload records or coordinate/SMILES spans from verified Job
instructions. This owner supplies no external interpreter or format/unit inference.
"""

from tools.container_guard import require_container

require_container()

import asyncio
import secrets
from contextlib import asynccontextmanager
from enum import Enum, auto

from nmrnet_provider.geometry_protocol import require_read_options
from nmrnet_provider.input_geometry import InputRejected, UploadSource
from nmrnet_provider.prepared_input import GeneratedJobInput, PreparedJobInput
from nmrnet_provider.retirement import retire_worker_channel
from nmrnet_provider.smiles_protocol import require_generation_deadline
from nmrnet_provider.submission import select_job_pasted, select_job_smiles
from nmrnet_provider.worker_protocol import require_seconds
from provider_core.job_input import VerifiedJobInput


class PreparationRetirementFailed(RuntimeError):
    """Safe caller text with ordered private cleanup causes."""

    def __init__(self, failures, *, operation_failure=None):
        super().__init__("Input resource retirement is unconfirmed; preserve its allocations and private cleanup evidence.")
        self.failures = tuple(failures)
        self.operation_failure = operation_failure
        self.cause = operation_failure if operation_failure is not None else self.failures[0][1]


class _Phase(Enum):
    NEW = auto()
    CHOOSING = auto()
    SUBMITTED = auto()
    FINISHED = auto()
    RETIRING = auto()
    RETIRED = auto()


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
        if type(max_worker_calls) is not int or max_worker_calls <= 0:
            raise ValueError("Cannot configure input preparation: the worker call limit must be a positive integer.")
        require_seconds(seconds)
        require_generation_deadline(generation_settings, generation_seconds)
        self._uploads = uploads
        self._transports = (("source_api_retirement", source_api_transport), ("source_store_retirement", source_store_transport))
        self._allocate_reader = allocate_reader
        self._allocate_generator = allocate_generator
        self._generation_settings, self._generation_seconds = generation_settings, generation_seconds
        self._chooser = chooser
        self._max_worker_calls, self._seconds = max_worker_calls, seconds
        self._worker_calls = 0
        self._phase = _Phase.NEW
        self._busy = False
        self._source = None
        self._source_task = None
        self._worker_task = None
        self._instructions = None
        self._candidate = None
        self._reference = None
        self.last_retirement_failure = None
        self.allocation_failure = None
        self.source_failure = None

    async def prepare(self, instructions):
        if self._phase is not _Phase.NEW:
            raise ValueError("Input preparation already started or retired; create a new Attempt-owned preparation session.")
        if type(instructions) is not VerifiedJobInput or instructions.job_ref != self._uploads.job_ref:
            raise ValueError("Input preparation requires the verified instructions for its Job's Uploads.")
        self._phase = _Phase.CHOOSING
        self._instructions = instructions
        try:
            async with asyncio.timeout(self._seconds):
                await self._chooser.run(instructions, self)
            if self._phase is not _Phase.SUBMITTED:
                raise ValueError("Input preparation did not submit a prepared reference before finishing or retirement.")
            return self._candidate
        finally:
            if self._phase in (_Phase.CHOOSING, _Phase.SUBMITTED):
                self._phase = _Phase.FINISHED

    def _require_choosing(self):
        if self._phase is not _Phase.CHOOSING:
            raise ValueError("Input choice is not active: it has not started, was submitted or is retiring.")

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
            self._require_choosing()
        finally:
            self._busy = False

    async def inspect(self):
        """Expose the actual Upload membership observation, including descriptions."""
        async with self._operation():
            return await self._source_call(self._uploads.inspect)

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
        if not task.cancelled():
            self.source_failure = task.exception()

    async def prepare_upload(self, upload_ref, *, source_format, source_unit, selection, alternate_location=None):
        """Read an explicit selection; return a reference only after reader retirement."""
        async with self._operation():
            self._candidate = self._reference = None
            require_read_options(source_format, source_unit, selection, alternate_location)
            self._require_worker_budget()
            if self._source is None or self._source.upload_ref != upload_ref:
                self._source = await self._source_call(self._uploads.acquire, upload_ref)
            async with self._worker(self._allocate_reader) as reader:
                geometries = await reader.read(
                    self._source.data,
                    source_format=source_format,
                    source_unit=source_unit,
                    selection=selection,
                    alternate_location=alternate_location,
                )
            self._require_choosing()
            source = self._source
            method = source_format
            if source_format == "json":
                method = "direct_coordinate_json"
            elif source_format == "table":
                method = "coordinate_table"
            self._candidate = PreparedJobInput(
                geometries,
                method,
                UploadSource(source.job_ref, source.upload_ref, source.byte_length, source.content_hash.removeprefix("sha256:")),
            )
            self._reference = secrets.token_urlsafe(24)
            return self._reference

    async def prepare_pasted(self, *, start_byte, end_byte, source_format, source_unit):
        """Read one exact Job span; line positions are relative to that span."""
        async with self._operation():
            self._candidate = self._reference = None
            selected = select_job_pasted(
                self._instructions,
                start_byte=start_byte,
                end_byte=end_byte,
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
            self._candidate = PreparedJobInput(geometries, method, selected)
            self._reference = secrets.token_urlsafe(24)
            return self._reference

    async def prepare_smiles(self, *, start_byte, end_byte, requested_count):
        """Generate one explicit Job selection; return a reference after retirement."""
        async with self._operation():
            self._candidate = self._reference = None
            selected = select_job_smiles(
                self._instructions, start_byte=start_byte, end_byte=end_byte, requested_count=requested_count
            )
            self._require_worker_budget()
            async with self._worker(
                self._allocate_generator, settings=self._generation_settings, operation_seconds=self._generation_seconds
            ) as generator:
                generated = await generator.generate(selected.source_bytes, selected.requested_count)
            self._require_choosing()
            self._candidate = GeneratedJobInput(selected, generated)
            self._reference = secrets.token_urlsafe(24)
            return self._reference

    def _require_worker_budget(self):
        if self._worker_calls >= self._max_worker_calls:
            raise RuntimeError("Cannot prepare another input: this conversation's worker call limit is exhausted.")

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

    def submit(self, reference):
        """Consume this conversation's current reference without reading or generating again."""
        self._require_choosing()
        if self._busy or self._worker_task is not None or self._source_task is not None:
            raise ValueError("Cannot submit input while source work or worker retirement remains unresolved.")
        if type(reference) is not str or self._reference is None or reference != self._reference:
            raise ValueError("Cannot submit input: the prepared reference does not belong to the current selection.")
        self._reference = None
        self._phase = _Phase.SUBMITTED

    def _record_allocation_failure(self, task):
        if not task.cancelled():
            self.allocation_failure = task.exception()

    async def _retire_worker(self):
        if self._worker_task is None:
            return
        async with asyncio.timeout(self._seconds):
            client, host = await asyncio.shield(self._worker_task)
        failures = await retire_worker_channel(client, host)
        if failures:
            self._raise_retirement_failure(failures)
        self._worker_task = None

    async def _settle_source(self):
        """Stop the whole acquisition before closing its reusable HTTP transports."""
        task = self._source_task
        if task is None:
            return
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
            # A settled acquisition failure stays on source_failure. Its HTTP
            # owners below still have to confirm their independent I/O cleanup.
            if not task.done():
                raise
        self._source_task = None

    def _raise_retirement_failure(self, failures, *, operation_failure=None):
        self.last_retirement_failure = PreparationRetirementFailed(failures, operation_failure=operation_failure)
        if isinstance(operation_failure, asyncio.CancelledError):
            raise operation_failure from self.last_retirement_failure
        for _, cause in failures:
            if isinstance(cause, asyncio.CancelledError):
                raise cause from self.last_retirement_failure
        raise self.last_retirement_failure from None

    async def retire(self):
        """Retire every granted owner; keep failed obligations for later observation."""
        if self._phase is _Phase.RETIRED:
            return
        self._phase = _Phase.RETIRING
        self._reference = None
        failures = []
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
