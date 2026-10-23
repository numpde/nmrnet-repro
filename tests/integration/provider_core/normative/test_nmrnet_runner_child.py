"""The existing molecular reader runs through fresh supervised native children."""

import json
import sys
import unittest
from unittest.mock import Mock

from nmrnet_provider.geometry_client import GeometryClient
from nmrnet_provider.native_session import NativeOperation
from tests.fakes.runner import RunningSupervisor


class BoundNativeChildTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_supplied_tables_preserve_atom_order_through_the_real_reader(self):
        # This lane installs core and mounts the binding under /consumer. Native
        # image qualification separately proves its -I image-owned installation.
        bootstrap = "import sys; sys.path.append('/consumer'); from nmrnet_provider.native_child import main; main()"
        runner = RunningSupervisor(
            role="reader", worker_argv=[sys.executable, "-c", bootstrap, "reader", "--io-seconds", "2"]
        )
        self.addCleanup(runner.close)
        identities = []
        for source in (b"H 0 0 0\nC 1.09 0 0\n", b"C 0 0 0\nH 0 1.09 0\n"):
            operation = NativeOperation(role="reader", endpoint=runner.path, operation_seconds=2, on_notice=Mock())
            reader, writer = await operation.start()
            client = GeometryClient(reader, writer, allocation=operation.allocation, operation_seconds=2)
            try:
                (geometry,) = await client.read(source, source_format="table", source_unit="angstrom", selection=None)
                packet = json.loads(geometry.request_bytes)
                self.assertEqual(packet["atoms"], [row.split()[0].decode() for row in source.splitlines()])
                self.assertEqual(geometry.source_positions, (1, 2))
                self.assertEqual(packet["coordinates"][1], [1.09, 0, 0] if source.startswith(b"H") else [0, 1.09, 0])
                identities.append(operation.allocation)
            except Exception as primary:
                runner.close()
                primary.add_note("Disposable native fixture stderr:\n" + runner.diagnostic.decode("utf-8", errors="replace")[-8192:])
                raise
            finally:
                failures = await operation.retire_channel(client)
            self.assertEqual(failures, [])
            await operation.settle()
        self.assertNotEqual(*identities)
        self.assertEqual(list(runner.scratch.iterdir()), [])
