"""Controller confirmation requires host evidence and completed local I/O."""

from tools.container_guard import require_container

require_container()

import asyncio
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from nmrnet_provider.retirement import HostRetirement, RetirementUnconfirmed


class HostRetirementTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.requests = self.root / "requests"
        self.receipts = self.root / "receipts"
        self.requests.mkdir()
        self.receipts.mkdir()
        self.owner = HostRetirement(
            allocation="allocation-1", request_directory=self.requests,
            receipt_directory=self.receipts, operation_seconds=0.1,
        )

    async def test_exact_confirmation_publishes_once(self):
        (self.receipts / "result").write_bytes(b"retired allocation-1\n")
        await self.owner.retire()
        await self.owner.retire()
        self.assertEqual((self.requests / "retire").read_bytes(), b"retire allocation-1\n")

    async def test_existing_request_is_not_adopted(self):
        (self.requests / "retire").write_bytes(b"retire allocation-1\n")
        (self.receipts / "result").write_bytes(b"retired allocation-1\n")
        with self.assertRaises(RetirementUnconfirmed) as first:
            await self.owner.retire()
        self.assertIsInstance(first.exception.__cause__, FileExistsError)
        (self.requests / "retire").unlink()
        with self.assertRaises(RetirementUnconfirmed) as second:
            await self.owner.retire()
        self.assertIs(first.exception, second.exception)
        self.assertFalse((self.requests / "retire").exists())

    async def test_refusal_can_be_observed_again_without_republishing(self):
        receipt = self.receipts / "result"
        receipt.write_bytes(b"unconfirmed allocation-1\n")
        with self.assertRaises(RetirementUnconfirmed):
            await self.owner.retire()
        receipt.write_bytes(b"retired allocation-1\n")
        await self.owner.retire()

    async def test_wrong_malformed_and_nonregular_receipts_refuse(self):
        for value in (b"retired other\n", b"retired allocation-1\nextra", b"x" * 300):
            with self.subTest(value=value[:30]):
                (self.receipts / "result").write_bytes(value)
                with self.assertRaises(RetirementUnconfirmed):
                    await self.owner.retire()
        (self.receipts / "result").unlink()
        os.mkfifo(self.receipts / "result")
        with self.assertRaises(RetirementUnconfirmed):
            await self.owner.retire()

    async def test_cancelled_caller_retains_original_io_until_completion(self):
        entered, release = threading.Event(), threading.Event()

        def held_read():
            entered.set()
            release.wait()
            return b"retired allocation-1\n"

        with patch.object(self.owner, "_read_receipt", side_effect=held_read) as read:
            caller = asyncio.create_task(self.owner.retire())
            try:
                async with asyncio.timeout(2):
                    while not entered.is_set():
                        await asyncio.sleep(0.005)
                caller.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await caller
                original = self.owner._operation
                with self.assertRaises(RetirementUnconfirmed):
                    await self.owner.retire()
                self.assertIs(self.owner._operation, original)
                self.assertFalse(original.done())
                self.assertEqual(read.call_count, 1)
            finally:
                release.set()
                if self.owner._operation is not None:
                    await self.owner._operation
            await self.owner.retire()

    async def test_receipt_read_failure_keeps_private_cause(self):
        (self.receipts / "result").write_bytes(b"retired allocation-1\n")
        cause = OSError("private-path-canary")
        with (
            patch("nmrnet_provider.retirement.os.read", side_effect=cause),
            self.assertRaises(RetirementUnconfirmed) as failure,
        ):
            await self.owner.retire()
        self.assertIs(failure.exception.__cause__, cause)
        self.assertNotIn("private-path-canary", str(failure.exception))
        await self.owner.retire()

    async def test_cancelled_publication_retains_late_failure_without_retry(self):
        entered, release = threading.Event(), threading.Event()
        cause = OSError("private-write-canary")

        def held_publish():
            entered.set()
            release.wait()
            raise cause

        with patch.object(self.owner, "_publish_request", side_effect=held_publish) as publish:
            caller = asyncio.create_task(self.owner.retire())
            try:
                async with asyncio.timeout(2):
                    while not entered.is_set():
                        await asyncio.sleep(0.005)
                caller.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await caller
                original = self.owner._operation
                with self.assertRaises(RetirementUnconfirmed):
                    await self.owner.retire()
                self.assertIs(original, self.owner._operation)
            finally:
                release.set()
                if self.owner._operation is not None:
                    with self.assertRaises(RetirementUnconfirmed):
                        await self.owner._operation
            await asyncio.sleep(0)
            self.assertIs(self.owner.last_failure.__cause__, cause)
            with self.assertRaises(RetirementUnconfirmed):
                await self.owner.retire()
            self.assertEqual(publish.call_count, 1)

    async def test_failed_receipt_close_cannot_be_erased_by_later_confirmation(self):
        (self.receipts / "result").write_bytes(b"retired allocation-1\n")
        close = os.close
        cause = OSError("private-close-canary")

        def failed_close(descriptor):
            close(descriptor)
            raise cause

        with (
            patch("nmrnet_provider.retirement.os.close", side_effect=failed_close),
            self.assertRaises(RetirementUnconfirmed) as failure,
        ):
            await self.owner.retire()
        self.assertIs(failure.exception.__cause__, cause)
        self.assertNotIn("private-close-canary", str(failure.exception))
        with self.assertRaises(RetirementUnconfirmed) as repeated:
            await self.owner.retire()
        self.assertIs(failure.exception, repeated.exception)
