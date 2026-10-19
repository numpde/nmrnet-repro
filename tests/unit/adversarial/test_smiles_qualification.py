"""A controlled stall must be observed before timeout can qualify retirement."""

from tools.container_guard import require_container

require_container()

import asyncio
import errno
import unittest
from builtins import BaseExceptionGroup
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from tests.integration.provider_worker import retirement_probe

from nmrnet_provider.worker_client import WorkerReportedFailure

with patch.dict("sys.modules", {"probe": retirement_probe}):
    from tests.integration.provider_worker import smiles_probe


class SmilesQualificationAdversarialTests(unittest.IsolatedAsyncioTestCase):
    def test_unknown_scenario_does_not_read_any_fixture(self):
        with patch.object(Path, "read_bytes") as read, self.assertRaises(ValueError):
            smiles_probe.source_case("unknown")
        read.assert_not_called()

    async def test_missing_or_foreign_predeadline_marker_fails_and_observes_cancelled_generation(self):
        waiting = asyncio.Event()

        async def generate(_source, _count):
            await waiting.wait()

        for marker in (None, b"foreign\n"):
            client = Mock(generate=AsyncMock(side_effect=generate))
            # silent-defaults: allow-contract -- A missing marker models the bounded wait expiring; a foreign marker is delivered unchanged.
            failure = TimeoutError("marker did not arrive") if marker is None else None
            case = Mock(source=b"CCl", requested_count=1)
            with (
                patch.object(smiles_probe, "wait_for", return_value=marker, side_effect=failure),
                self.assertRaises((TimeoutError, RuntimeError)),
            ):
                await smiles_probe.generate_case(client, case, "allocation", "geometry-smiles-timeout")
            self.assertFalse(any(task is not asyncio.current_task() and not task.done() for task in asyncio.all_tasks()))

    async def test_a_timeout_before_observing_the_marker_cannot_qualify_a_controlled_stall(self):
        client = Mock(generate=AsyncMock(side_effect=TimeoutError("generation expired")))
        case = Mock(source=b"CCl", requested_count=1)
        with patch.object(smiles_probe, "wait_for", return_value=b"allocation\n"), self.assertRaisesRegex(RuntimeError, "before"):
            await smiles_probe.generate_case(client, case, "allocation", "geometry-smiles-timeout")

    async def test_a_foreign_marker_keeps_a_simultaneous_private_generation_failure(self):
        client = Mock(generate=AsyncMock(side_effect=WorkerReportedFailure("PRIVATE_NATIVE_CAUSE")))
        case = Mock(source=b"CCl", requested_count=1)
        with patch.object(smiles_probe, "wait_for", return_value=b"foreign\n"), self.assertRaises(RuntimeError) as caught:
            await smiles_probe.generate_case(client, case, "allocation", "geometry-smiles-timeout")
        self.assertIn("PRIVATE_NATIVE_CAUSE", "\n".join(caught.exception.__notes__))

    async def test_unconfirmed_retirement_cannot_publish_success_evidence(self):
        client = Mock(generate=AsyncMock(return_value=object()), close=Mock(), wait_closed=AsyncMock())
        host = Mock(retire=AsyncMock(side_effect=RuntimeError("retirement unconfirmed")))
        with (
            patch.object(Path, "exists", return_value=False),
            patch.object(Path, "write_bytes", side_effect=OSError(errno.EROFS, "read-only host receipt")),
            patch.object(Path, "write_text") as write,
            patch.object(smiles_probe, "source_case", return_value=Mock()),
            patch.object(smiles_probe, "generate_case", new=AsyncMock(return_value=object())),
            patch.object(smiles_probe.asyncio, "open_unix_connection", new=AsyncMock(return_value=(Mock(), Mock()))),
            patch.object(smiles_probe, "SmilesClient", return_value=client),
            patch.object(smiles_probe, "HostRetirement", return_value=host),
            self.assertRaises(BaseExceptionGroup),
        ):
            await smiles_probe.controller("allocation", "geometry-smiles")
        write.assert_not_called()
        host.retire.assert_awaited_once()
        client.close.assert_called()
        client.wait_closed.assert_awaited_once()
