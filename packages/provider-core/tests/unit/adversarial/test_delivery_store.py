"""Queue refusal preserves the original outstanding request and its owner."""

import tempfile
import errno
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from provider_core.delivery_store import DeliveryConflict, DeliveryOutcome, DeliveryQueue, DeliveryStorageFailed, QueueLimits
from tests.fakes.delivery import outcome


class DeliveryQueueRefusalTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.limits = QueueLimits(provider_items=1, provider_bytes=4096, total_items=2, total_bytes=8192)

    def open(self):
        queue = DeliveryQueue(self.root, limits=self.limits)
        self.addCleanup(queue.close)
        return queue

    def test_conflicting_body_cannot_replace_or_remove_the_original(self):
        queue = self.open()
        original, other = outcome(), outcome(message="different accepted candidate")
        queue.enqueue(original)
        with self.assertRaises(DeliveryConflict):
            queue.enqueue(other)
        with self.assertRaises(DeliveryConflict):
            queue.remove_published(other)
        self.assertEqual(queue.next_pending(original.api_origin, original.provider_ref), original)

    def test_corrupt_database_is_not_replaced(self):
        path = self.root / "delivery.sqlite3"
        path.write_bytes(b"not a SQLite database")
        path.chmod(0o600)
        with self.assertRaises(DeliveryStorageFailed):
            self.open()
        self.assertEqual(path.read_bytes(), b"not a SQLite database")

    def test_one_service_owns_the_database(self):
        first = self.open()
        item = outcome()
        first.enqueue(item)
        with self.assertRaises(DeliveryStorageFailed):
            self.open()
        self.assertEqual(first.next_pending(item.api_origin, item.provider_ref), item)

    def test_body_cannot_claim_another_attempt(self):
        item = outcome()
        with self.assertRaises(ValueError):
            DeliveryOutcome(
                item.api_origin, item.provider_ref, item.job_ref, item.execution_attempt_ref[:-1] + "1",
                item.operation, item.body,
            )

    def test_missing_volume_directory_keeps_operation_and_original_cause(self):
        with self.assertRaises(DeliveryStorageFailed) as caught:
            DeliveryQueue(self.root / "missing", limits=self.limits)
        self.assertIn("open delivery storage", str(caught.exception))
        self.assertIsInstance(caught.exception.cause, FileNotFoundError)
        self.assertFalse((self.root / "missing").exists())

    def test_failed_descriptor_inspection_closes_the_original_file_before_handoff(self):
        opened = []
        real_open, real_fstat = os.open, os.fstat
        primary = OSError(errno.EIO, "PRIVATE_NATIVE")

        def open_original(*arguments):
            descriptor = real_open(*arguments)
            opened.append(descriptor)
            return descriptor

        with patch("provider_core.delivery_store.os.open", side_effect=open_original), \
                patch("provider_core.delivery_store.os.fstat", side_effect=primary):
            with self.assertRaises(DeliveryStorageFailed) as refused:
                self.open()
        self.assertEqual(len(opened), 1)
        with self.assertRaises(OSError) as closed:
            real_fstat(opened[0])
        self.assertEqual(closed.exception.errno, errno.EBADF)
        self.assertIs(refused.exception.cause, primary)
        self.assertIn(".owner.lock", str(refused.exception))
        self.assertNotIn("PRIVATE", str(refused.exception))
