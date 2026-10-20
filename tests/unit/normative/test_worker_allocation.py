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
    def test_native_grants_use_frozen_code_and_declarations_with_separate_prepared_artifacts(self):
        for role in ("prediction", "reader", "smiles"):
            with self.subTest(role=role):
                host = HostFixture()
                self.addCleanup(host.close)
                result = host.run(
                    f"selected_role={role}\n"
                    r"""
provider_worker_source_root="$FAKE_STATE/frozen"
provider_worker_prepared_root="$FAKE_STATE/prepared"
provider_workers_open "$FAKE_ROOT" 3 1
mkdir -m 700 -- "$FAKE_ROOT/requests/$FAKE_TOKEN"
printf 'allocate %s %s\n' "$selected_role" "$FAKE_TOKEN" > "$FAKE_ROOT/requests/$FAKE_TOKEN/allocate"
provider_workers_service
provider_workers_retire "$FAKE_TOKEN"
provider_workers_close
"""
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                create = host.commands()[0]
                mounts = [create[index + 1].decode() for index, word in enumerate(create) if word == b"--mount"]
                self.assertIn(f"type=bind,src={host.root}/frozen/nmrnet_provider,dst=/workspace/nmrnet_provider,readonly", mounts)
                self.assertTrue(all(f"src={Path.cwd()}/" not in mount for mount in mounts))
                if role == "prediction":
                    for source, target in (
                        ("artifacts/prediction-models.json", "/input/prediction-models.json"),
                        ("requirements/inference.lock", "/input/inference.lock"),
                        ("requirements/unicore-source.json", "/input/unicore-source.json"),
                    ):
                        self.assertIn(f"type=bind,src={host.root}/frozen/{source},dst={target},readonly", mounts)
                    for name in ("weight", "checkpoints"):
                        self.assertIn(f"type=bind,src={host.root}/prepared/{name},dst=/{name},readonly", mounts)

    def test_each_role_receives_its_own_io_budget_and_smiles_keeps_its_embedding_budget(self):
        for role, seconds in (("prediction", 13), ("reader", 2), ("smiles", 7)):
            with self.subTest(role=role):
                host = HostFixture()
                self.addCleanup(host.close)
                result = host.run(
                    f"selected_role={role}\n"
                    r"""
provider_worker_io_seconds=13 provider_source_io_seconds=2 provider_generation_io_seconds=7
provider_worker_embedding_seconds=3
provider_workers_open "$FAKE_ROOT" 3 1
mkdir -m 700 -- "$FAKE_ROOT/requests/$FAKE_TOKEN"
printf 'allocate %s %s\n' "$selected_role" "$FAKE_TOKEN" > "$FAKE_ROOT/requests/$FAKE_TOKEN/allocate"
provider_workers_service
provider_workers_retire "$FAKE_TOKEN"
provider_workers_close
"""
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                create = host.commands()[0]
                self.assertEqual(create[create.index(b"--io-seconds") + 1], str(seconds).encode())
                if role == "smiles":
                    self.assertEqual(create[create.index(b"--embedding-seconds") + 1], b"3")
                else:
                    self.assertNotIn(b"--embedding-seconds", create)

    def test_host_grants_and_receipts_are_private_without_changing_the_callers_umask(self):
        for mask in ("000", "022", "077"):
            with self.subTest(umask=mask):
                host = HostFixture()
                self.addCleanup(host.close)
                result = host.run(
                    f"umask {mask}\n"
                    r"""
original_mask=$(umask)
provider_workers_open "$FAKE_ROOT" 3 1
mkdir -m 700 -- "$FAKE_ROOT/requests/$FAKE_TOKEN"
printf 'allocate reader %s\n' "$FAKE_TOKEN" > "$FAKE_ROOT/requests/$FAKE_TOKEN/allocate"
provider_workers_service
[[ $(umask) == "$original_mask" ]]
[[ $(stat -c '%a' -- "$FAKE_ROOT/ipc/$FAKE_TOKEN") == 700 ]]
provider_workers_retire "$FAKE_TOKEN"
unallocated=dddddddddddddddddddddddddddddddd
mkdir -m 700 -- "$FAKE_ROOT/requests/$unallocated"
printf 'retire %s\n' "$unallocated" > "$FAKE_ROOT/requests/$unallocated/retire"
provider_workers_service
[[ $(umask) == "$original_mask" ]]
provider_workers_close
[[ $(umask) == "$original_mask" ]]
"""
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    [row[0] for row in host.commands()], [b"create", b"start", b"inspect", b"stop", b"inspect", b"rm"]
                )
                root = host.allocation_root
                directories = [root, *(root / name for name in ("requests", "receipts", "ipc", "owned"))]
                directories.extend(path for path in (root / "owned").rglob("*") if path.is_dir())
                directories.extend(path for path in (root / "receipts").iterdir() if path.is_dir())
                for directory in directories:
                    self.assertEqual(directory.stat().st_mode & 0o777, 0o700, str(directory.relative_to(root)))
                files = [*(root / "owned").rglob("*"), *(root / "receipts").rglob("*")]
                for path in files:
                    if path.is_file():
                        self.assertEqual(path.stat().st_mode & 0o777, 0o600, str(path.relative_to(root)))

    def test_completed_report_is_retained_before_retirement_without_a_grace_delay(self):
        host = HostFixture()
        self.addCleanup(host.close)
        result = host.run("""
provider_workers_open "$FAKE_ROOT" 3 1
mkdir -- "$FAKE_ROOT/requests/$FAKE_TOKEN"
printf 'allocate reader %s\n' "$FAKE_TOKEN" > "$FAKE_ROOT/requests/$FAKE_TOKEN/allocate"
provider_workers_service
printf '{"allocation":"%s"}\n' "$FAKE_TOKEN" > "$FAKE_ROOT/owned/$FAKE_TOKEN/diagnostics/resources.json"
sleep() { if [[ $1 == 0.05 ]]; then printf 'unexpected delay\n' >&2; return 91; fi; command sleep "$@"; }
printf 'retire %s\n' "$FAKE_TOKEN" > "$FAKE_ROOT/requests/$FAKE_TOKEN/retire"
provider_workers_service
cmp -- "$FAKE_ROOT/owned/$FAKE_TOKEN/diagnostics/resources.json" "$FAKE_ROOT/receipts/$FAKE_TOKEN/resources.json"
provider_workers_close
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            (host.allocation_root / "receipts" / HOST_TOKEN / "result").read_bytes(), f"retired {HOST_TOKEN}\n".encode()
        )

    def test_missing_report_exhausts_only_the_normal_grace_then_still_retires(self):
        host = HostFixture()
        self.addCleanup(host.close)
        result = host.run("""
provider_workers_open "$FAKE_ROOT" 3 1
mkdir -- "$FAKE_ROOT/requests/$FAKE_TOKEN"
printf 'allocate reader %s\n' "$FAKE_TOKEN" > "$FAKE_ROOT/requests/$FAKE_TOKEN/allocate"
provider_workers_service
SECONDS=0
sleep() { if [[ $1 == 0.05 ]]; then printf '%s\n' "$1" >> "$FAKE_STATE/pauses"; SECONDS=2; else command sleep "$@"; fi; }
printf 'retire %s\n' "$FAKE_TOKEN" > "$FAKE_ROOT/requests/$FAKE_TOKEN/retire"
provider_workers_service
provider_workers_close
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((host.root / "pauses").read_text().splitlines(), ["0.05"])
        self.assertFalse((host.allocation_root / "receipts" / HOST_TOKEN / "resources.json").exists())
        self.assertEqual(host.commands()[-1][0], b"rm")

    def test_emergency_retirement_does_not_wait_for_a_report(self):
        host = HostFixture()
        self.addCleanup(host.close)
        result = host.run("""
provider_workers_open "$FAKE_ROOT" 3 1
mkdir -- "$FAKE_ROOT/requests/$FAKE_TOKEN"
printf 'allocate reader %s\n' "$FAKE_TOKEN" > "$FAKE_ROOT/requests/$FAKE_TOKEN/allocate"
provider_workers_service
sleep() { if [[ $1 == 0.05 ]]; then printf 'unexpected delay\n' >&2; return 91; fi; command sleep "$@"; }
provider_workers_retire "$FAKE_TOKEN"
provider_workers_close
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(host.commands()[-1][0], b"rm")

    def test_fixed_reader_create_then_exact_retirement_releases_only_owned_grants(self):
        host = HostFixture()
        self.addCleanup(host.close)
        result = host.run("""
provider_workers_open "$FAKE_ROOT" 3 1
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
