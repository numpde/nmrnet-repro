"""Separate processes cannot concurrently own one retained Attempt directory."""

import asyncio
import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

from provider_core.attempt_store import AttemptStore, StoreLimits
from tests.fakes.attempt_store import record, scope


class AttemptStoreProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_second_process_is_excluded_then_recovers_after_owner_closes(self):
        child = """
import asyncio, json, sys
from pathlib import Path
from provider_core.attempt_record import RecoveryScope
from provider_core.attempt_store import AttemptStore, StoreLimits, StoreRefused

async def main():
    store = AttemptStore(Path(sys.argv[1]), scope=RecoveryScope(**json.loads(sys.argv[2])),
                         limits=StoreLimits(2, 4194304, 0), operation_seconds=1)
    try:
        try:
            await store.open()
        except StoreRefused:
            print('locked')
        else:
            print(len(await store.records()))
    finally:
        await store.close()

asyncio.run(main())
"""
        with tempfile.TemporaryDirectory() as directory:
            store = AttemptStore(Path(directory), scope=scope(), limits=StoreLimits(2, 4194304, 0), operation_seconds=1)
            arguments = [sys.executable, "-I", "-B", "-c", child, directory, json.dumps(asdict(scope()))]
            await store.open()
            try:
                await store.admit(record())
                denied = await asyncio.to_thread(subprocess.run, arguments, capture_output=True, text=True, timeout=5)
                self.assertEqual(denied.returncode, 0, denied.stderr)
                self.assertEqual(denied.stdout, "locked\n")
            finally:
                await store.close()
            recovered = await asyncio.to_thread(subprocess.run, arguments, capture_output=True, text=True, timeout=5)
            self.assertEqual(recovered.returncode, 0, recovered.stderr)
            self.assertEqual(recovered.stdout, "1\n")
