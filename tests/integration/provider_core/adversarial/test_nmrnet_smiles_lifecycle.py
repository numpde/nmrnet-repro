"""The native fixture owns prestarted hosts even when no input client is allocated."""

import asyncio
import json
import tempfile
import unittest
from builtins import ExceptionGroup
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from tests.fakes.attempt_store import scope
from tests.fakes.job_input import facts
from tests.integration.nmrnet.native_lifecycle import (
    PrestartedSourcesRetirement,
    require_host_source_closure,
)
from tests.integration.nmrnet.normative import test_nmrnet_smiles_lifecycle as native_lifecycle
from tests.integration.nmrnet.test_live_worker import CheckedRetirement


class PrestartedSourceCleanupTests(unittest.IsolatedAsyncioTestCase):
    def test_mounted_sources_require_matching_host_manifest_entries(self):
        execution = {"tool_sources": {"tools/example.py": {"sha256": "a" * 64}}}
        matching = ("a" * 64 + "  tools/example.py\n").encode()
        require_host_source_closure(execution, matching)
        for manifest in (b"", ("b" * 64 + "  tools/example.py\n").encode()):
            with self.subTest(manifest=manifest), self.assertRaisesRegex(AssertionError, "tools/example.py"):
                require_host_source_closure(execution, manifest)

    def setUp(self):
        self.events = []

        async def first():
            self.events.append("first source")

        async def second():
            self.events.append("second source")

        self.sources = (
            SimpleNamespace(retire=AsyncMock(side_effect=first)),
            SimpleNamespace(retire=AsyncMock(side_effect=second)),
        )
        self.owner = PrestartedSourcesRetirement(
            source_hosts=self.sources,
            allocation="fixture",
            request_directory=Path("/unused/request"),
            receipt_directory=Path("/unused/receipt"),
            operation_seconds=1,
        )

    async def test_all_prestarted_sources_retire_before_science_without_allocating_clients(self):
        async def science():
            self.events.append("science")

        with patch.object(CheckedRetirement, "retire", new=AsyncMock(side_effect=science)) as normal:
            await self.owner.retire()
        self.assertEqual(self.events, ["first source", "second source", "science"])
        normal.assert_awaited_once()

    async def test_source_failure_does_not_skip_other_sources_or_prediction_cleanup(self):
        first, second = OSError("private source cleanup"), OSError("private science cleanup")
        self.sources[0].retire.side_effect = first
        with (
            patch.object(CheckedRetirement, "retire", new=AsyncMock(side_effect=second)) as normal,
            self.assertRaises(ExceptionGroup) as caught,
        ):
            await self.owner.retire()
        self.sources[1].retire.assert_awaited_once()
        normal.assert_awaited_once()
        self.assertEqual(caught.exception.exceptions, (first, second))

    async def test_source_cancellation_keeps_prior_cause_and_later_host_failure(self):
        cancelled, cleanup = asyncio.CancelledError(), OSError("private science cleanup")
        previous = OSError("private earlier cancellation cause")
        cancelled.__cause__ = previous
        self.sources[0].retire.side_effect = cancelled
        with (
            patch.object(CheckedRetirement, "retire", new=AsyncMock(side_effect=cleanup)) as normal,
            self.assertRaises(asyncio.CancelledError) as caught,
        ):
            await self.owner.retire()
        self.assertIs(caught.exception, cancelled)
        self.assertEqual(cancelled.__cause__.exceptions, (cancelled, cleanup))
        self.assertIs(cancelled.__cause__.interruption_cause, previous)
        self.sources[1].retire.assert_awaited_once()
        normal.assert_awaited_once()


class NativeFixtureSetupCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_replay_open_failure_keeps_both_primary_and_close_failure_and_retires_api(self):
        native = native_lifecycle.LiveSmilesLifecycleTests()
        primary, cleanup = OSError("private replay open failure"), OSError("private replay close failure")
        store = SimpleNamespace(open=AsyncMock(side_effect=primary), close=AsyncMock(side_effect=cleanup), pending_operation=None)
        transport = SimpleNamespace(retire=AsyncMock())
        native.open_store = Mock(return_value=store)
        native.config = SimpleNamespace(api=SimpleNamespace(http=SimpleNamespace(new_transport=Mock(return_value=transport))))
        with self.assertRaises(ExceptionGroup) as caught:
            await native.replay_terminal(Path("/unused"), scope(), None)
        self.assertEqual(caught.exception.exceptions, (primary, cleanup))
        store.close.assert_awaited_once()
        transport.retire.assert_awaited_once()

    async def test_discovery_and_store_open_failures_still_retire_acquired_io(self):
        for phase in ("discovery", "store open"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                native = native_lifecycle.LiveSmilesLifecycleTests()
                primary = OSError("private fixture " + phase)
                transport = SimpleNamespace(retire=AsyncMock())
                api = SimpleNamespace(
                    jobs=AsyncMock(
                        return_value=SimpleNamespace(jobs=(SimpleNamespace(input=facts(), analysis_kind_ref="peaks_from_mol"),))
                    )
                )
                store = SimpleNamespace(open=AsyncMock(), close=AsyncMock(), pending_operation=None)
                if phase == "discovery":
                    api.jobs.side_effect = primary
                else:
                    store.open.side_effect = primary
                native.config = SimpleNamespace(
                    api=SimpleNamespace(identity=object(), http=SimpleNamespace(new_transport=Mock(return_value=transport))),
                    lifecycle=SimpleNamespace(scope=scope(), store_root=Path(directory) / "attempts"),
                )
                native.open_store = Mock(return_value=store)

                @asynccontextmanager
                async def peers():
                    yield SimpleNamespace(origin="https://api.example")

                native.configured_peers = peers
                startup = [
                    json.dumps({"normative": [{"name": "flexible_multiple"}]}).encode(),
                    b'{"runtime":{}}',
                    json.dumps(
                        {
                            "dictionary": dict.fromkeys(("archive_sha256", "member", "size", "sha256"), "fixture"),
                            "elements": {},
                            "checkpoint_archive": {"archive_sha256": "fixture"},
                        }
                    ).encode(),
                ]
                with (
                    patch("tests.integration.nmrnet.native_lifecycle.Path.read_bytes", side_effect=startup),
                    patch("tests.integration.nmrnet.native_lifecycle.ProviderAPI", return_value=api),
                    self.assertRaises(OSError) as caught,
                ):
                    await native.joined_lifecycle()
                self.assertIs(caught.exception, primary)
                transport.retire.assert_awaited_once()
                if phase == "store open":
                    store.close.assert_awaited_once()
                else:
                    native.open_store.assert_not_called()

    async def test_unadopted_worker_cleanup_observes_channel_and_host(self):
        native = native_lifecycle.LiveSmilesLifecycleTests()
        native.worker = SimpleNamespace(close=Mock(), wait_closed=AsyncMock())
        native.retirement = SimpleNamespace(retire=AsyncMock())
        await native.retire_unadopted_science()
        native.worker.close.assert_called_once()
        native.worker.wait_closed.assert_awaited_once()
        native.retirement.retire.assert_awaited_once()

    async def test_preflight_settlement_observes_only_the_callers_original_pending_io(self):
        native = native_lifecycle.LiveSmilesLifecycleTests()
        native.store = SimpleNamespace(pending_operation="read Attempt inventory", pending_owner=None, settle=AsyncMock())
        await native.settle_preflight()
        native.store.settle.assert_awaited_once()
        native.store.pending_owner = object()
        await native.settle_preflight()
        native.store.settle.assert_awaited_once()
