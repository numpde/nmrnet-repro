"""Verified ZIP member selection preserves source identity through input ownership."""

import asyncio
import hashlib
import io
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zipfile import ZIP_DEFLATED, ZipFile

from tests.fakes.uploads import UPLOAD_REF
from tests.integration.nmrnet.interpreter_ports import UploadPorts
from tests.integration.nmrnet.test_nmrnet_session import HostOwner

from nmrnet_provider.input_geometry import InputRejected, PreparedUploadMember
from nmrnet_provider.input_preparation import _Phase
from nmrnet_provider.upload_archive import inspect_upload, select_member
from nmrnet_provider.xyz import prepare_xyz
from provider_core.uploads import VerifiedUpload


class UploadPreparationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_source_wait_reports_late_evidence_after_settlement(self):
        ports = UploadPorts()
        entered = asyncio.Event()
        evidence = OSError("late source cleanup failure")
        observed = []

        async def acquire():
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                ports.uploads.recovery_failure = evidence
                raise

        owner = ports.owner(AsyncMock(), on_source_observation=observed.append)
        self.addAsyncCleanup(owner.retire)
        # This isolated source-lifetime test enters below the normal preparation
        # port, which supplies the phase permission in an assembled session.
        owner._phase = _Phase.CHOOSING
        owner._require_execution = lambda: None
        waiter = asyncio.create_task(owner._source_call(acquire))
        await entered.wait()
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.assertEqual(observed, [])
        await owner._settle_source()
        self.assertEqual(observed, [evidence])

    async def test_verified_source_is_owned_before_recovery_is_reported(self):
        ports = UploadPorts()
        failure = OSError("source temporarily unavailable")
        verified = ports.uploads.acquire.return_value
        observed = []

        async def acquire(_upload_ref):
            ports.uploads.recovery_failure = failure
            return verified

        async def choose(_instructions, capability):
            await capability.prepare_upload(
                UPLOAD_REF, source_format="xyz", source_unit="angstrom", selection=(1,)
            )

        def report(cause):
            observed.append((cause, owner._source))

        ports.uploads.acquire.side_effect = acquire
        owner = ports.owner(choose, on_source_observation=report)
        self.addAsyncCleanup(owner.retire)
        await owner.prepare(ports.instructions, require_execution=lambda: None)
        self.assertEqual(observed, [(failure, verified)])

    async def test_zip_inspection_correction_and_selection_keep_one_verified_snapshot(self):
        ports = UploadPorts()
        readers = []
        member_bytes = ports.source
        stream = io.BytesIO()
        with ZipFile(stream, "w", compression=ZIP_DEFLATED) as archive:
            archive.writestr("structures/two.xyz", member_bytes)
        zipped = stream.getvalue()
        ports.uploads.acquire.return_value = VerifiedUpload(
            "job:example", UPLOAD_REF, len(zipped), "sha256:" + hashlib.sha256(zipped).hexdigest(), zipped
        )

        async def allocate():
            async def read_member(source, *, member, **options):
                self.assertIs(source, zipped)
                selected = select_member(source, member)
                return PreparedUploadMember(
                    prepare_xyz(selected, source_unit=options["source_unit"], selection=options["selection"]), selected
                )

            reader = SimpleNamespace(
                inspect_upload=AsyncMock(side_effect=inspect_upload),
                read_member=read_member,
                close=Mock(),
                wait_closed=AsyncMock(),
            )
            host = HostOwner()
            host.retire = AsyncMock(wraps=host.retire)
            readers.append((reader, host))
            return reader, host

        async def choose(_instructions, capability):
            await capability.inspect()
            inventory = await capability.inspect_upload(UPLOAD_REF)
            self.assertEqual(inventory["content_hash"], "sha256:" + hashlib.sha256(zipped).hexdigest())
            self.assertEqual(inventory["members"], [{"name": "structures/two.xyz", "byte_length": len(member_bytes)}])
            readers[0][1].retire.assert_awaited_once()
            with self.assertRaisesRegex(InputRejected, "missing"):
                await capability.prepare_upload(
                    UPLOAD_REF, member="missing.xyz", source_format="xyz", source_unit="angstrom", selection=(2, 1)
                )
            readers[1][1].retire.assert_awaited_once()
            await capability.prepare_upload(
                UPLOAD_REF, member="structures/two.xyz", source_format="xyz", source_unit="angstrom", selection=(2, 1)
            )

        owner = ports.owner(choose, max_worker_calls=3, allocate_reader=allocate)
        self.addAsyncCleanup(owner.retire)
        prepared = await owner.prepare(ports.instructions, require_execution=lambda: None)
        self.assertEqual((2, 1), tuple(geometry.source_record_number for geometry in prepared.geometries))
        self.assertEqual(prepared.source.sha256, hashlib.sha256(member_bytes).hexdigest())
        self.assertEqual(prepared.source.upload.sha256, hashlib.sha256(zipped).hexdigest())
        self.assertEqual(prepared.source.member_name, "structures/two.xyz")
        ports.uploads.acquire.assert_awaited_once_with(UPLOAD_REF)
        self.assertEqual(len(readers), 3)
        for reader, host in readers:
            host.retire.assert_awaited_once()
            reader.wait_closed.assert_awaited_once()
