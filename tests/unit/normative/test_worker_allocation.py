"""Allocation requests carry only a fixed role and a fresh identity."""

from tools.container_guard import require_container

require_container()

import asyncio
import tempfile
import unittest
from pathlib import Path

from tests.unit.worker_host_fakes import CID, HostFixture
from tests.unit.worker_host_fakes import TOKEN as HOST_TOKEN

from nmrnet_provider.allocation import AllocationUnconfirmed, WorkerAllocation

TOKEN = "f" * 32


class WorkerAllocationNormativeTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_exact_request_and_host_receipt_can_be_observed_again_without_reissue(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("requests", "receipts"):
                (root / name).mkdir()
            allocation = WorkerAllocation(
                role="reader",
                allocation=TOKEN,
                request_root=root / "requests",
                receipt_root=root / "receipts",
                ipc_root=root / "ipc",
                operation_seconds=1,
            )
            operation = asyncio.create_task(allocation.start())
            try:
                async with asyncio.timeout(2):
                    request = root / "requests" / TOKEN / "allocate"
                    while not request.exists():
                        await asyncio.sleep(0.01)
                    self.assertEqual(request.read_bytes(), f"allocate reader {TOKEN}\n".encode())
                    receipt = root / "receipts" / TOKEN
                    receipt.mkdir()
                    (root / "ipc" / TOKEN).mkdir(parents=True)
                    (root / "ipc" / TOKEN / "ready").write_bytes((TOKEN + "\n").encode())
                    (receipt / "allocated").write_bytes(f"allocated reader {TOKEN}\n".encode())
                    await operation
                    await allocation.start()
                    self.assertEqual(list((root / "requests" / TOKEN).iterdir()), [request])
                    retirement = asyncio.create_task(allocation.retirement.retire())
                    while not (request.parent / "retire").exists():
                        await asyncio.sleep(0.01)
                    self.assertEqual((request.parent / "retire").read_bytes(), f"retire {TOKEN}\n".encode())
                    (receipt / "result").write_bytes(f"retired {TOKEN}\n".encode())
                    await retirement
                    await allocation.settle()
                    with self.assertRaisesRegex(AllocationUnconfirmed, "retir"):
                        await allocation.start()
            finally:
                if not operation.done():
                    operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)


class HostAllocationNormativeTests(unittest.TestCase):
    def test_fixed_reader_create_then_exact_retirement_releases_only_owned_grants(self):
        host = HostFixture()
        self.addCleanup(host.close)
        result = host.run("""
provider_workers_open "$FAKE_ROOT" 2
mkdir -- "$FAKE_ROOT/requests/$FAKE_TOKEN"
printf 'allocate reader %s\n' "$FAKE_TOKEN" > "$FAKE_ROOT/requests/$FAKE_TOKEN/allocate"
provider_workers_service
printf 'retire %s\n' "$FAKE_TOKEN" > "$FAKE_ROOT/requests/$FAKE_TOKEN/retire"
provider_workers_service
provider_workers_close
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            (host.allocation_root / "receipts" / HOST_TOKEN / "allocated").read_bytes(),
            f"allocated reader {HOST_TOKEN}\n".encode(),
        )
        self.assertEqual(
            (host.allocation_root / "receipts" / HOST_TOKEN / "result").read_bytes(), f"retired {HOST_TOKEN}\n".encode()
        )
        self.assertFalse((host.allocation_root / "ipc" / HOST_TOKEN).exists())
        commands = host.commands()
        self.assertEqual([row[0] for row in commands], [b"create", b"start", b"inspect", b"stop", b"inspect", b"rm"])
        self.assertIn(CID.encode(), commands[-1])
        create = commands[0]
        self.assertEqual(create[create.index(b"--network") + 1], b"none")
        self.assertIn(b"--read-only", create)
        self.assertIn(b"nmrnet_provider.worker_main", create)
        self.assertNotIn(b"/weight", b" ".join(create))
