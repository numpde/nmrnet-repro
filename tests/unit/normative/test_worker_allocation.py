"""Allocation requests carry only a fixed role and a fresh identity."""

from tools.container_guard import require_container

require_container()

import asyncio
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from tests.unit.worker_host_fakes import CID, HostFixture
from tests.unit.worker_host_fakes import TOKEN as HOST_TOKEN

from nmrnet_provider.allocation import AllocationUnconfirmed, WorkerAllocation, release_native_history

TOKEN = "f" * 32


class WorkerAllocationNormativeTests(unittest.IsolatedAsyncioTestCase):
    async def test_history_handoff_publishes_exact_frontier_and_waits_past_previous_acknowledgement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            requests, receipts = root / "requests", root / "receipts"
            requests.mkdir(mode=0o700)
            receipts.mkdir(mode=0o700)
            acknowledgement = receipts / ".history-released"
            acknowledgement.write_bytes(f"released {1:032x}\n".encode())
            task = asyncio.create_task(
                asyncio.to_thread(
                    release_native_history,
                    frontier=2,
                    previous=1,
                    request_root=requests,
                    receipt_root=receipts,
                    seconds=1,
                )
            )
            try:
                async with asyncio.timeout(1):
                    while not (requests / ".release-history").exists():
                        await asyncio.sleep(0.01)
                self.assertEqual((requests / ".release-history").read_bytes(), f"release {2:032x}\n".encode())
                self.assertEqual((requests / ".release-history").stat().st_mode & 0o777, 0o600)
                self.assertFalse((requests / ".release-history-pending").exists())
                self.assertFalse(task.done())
                acknowledgement.write_bytes(f"released {2:032x}\n".encode())
                self.assertEqual(await task, 2)
            finally:
                await task

    def test_native_launch_inherits_both_streams_without_diagnostic_dump_files(self):
        host = HostFixture()
        self.addCleanup(host.close)
        host.environment["NMRNET_LOG_LEVEL_INPUT"] = "DEBUG"
        result = host.run(r"""
provider_workers_open "$FAKE_ROOT" 3 1
mkdir -m 700 -- "$FAKE_ROOT/receipts/$FAKE_TOKEN"
provider_workers_create reader "$FAKE_TOKEN"
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        create = host.commands()[0]
        self.assertEqual(create[create.index(b"--log-driver") + 1], b"journald")
        for option in (b"mode=non-blocking", b"max-buffer-size=1m", b"tag=nmrnet-{{.FullID}}", b"NMRNET_LOG_LEVEL=DEBUG"):
            self.assertIn(option, create)
        command = create[create.index(b"-c") + 1].decode()
        self.assertNotIn("/diagnostics/stdout", command)
        self.assertNotIn("/diagnostics/stderr", command)
        run = subprocess.run(
            [
                "bash",
                "-eu",
                "-c",
                command,
                "worker",
                "sh",
                "-c",
                "printf 'routine-event\\n'; printf 'PRIVATE_COMPLETED_OUTCOMES\\n' >&2",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        self.assertEqual((run.returncode, run.stdout, run.stderr), (0, "routine-event\n", "PRIVATE_COMPLETED_OUTCOMES\n"))

    async def test_real_client_and_host_protocol_complete_five_channels_with_three_active_request_slots(self):
        host = HostFixture()
        self.addCleanup(host.close)
        program = r"""
umask 077
provider_workers_open "$FAKE_ROOT" 3 1
for iteration in {1..1000}; do
    provider_workers_service
    # This recording engine supplies only channel metadata/readiness, not
    # coordinates or model results. Real native science is qualified separately.
    for token in "${!provider_worker_roles[@]}"; do
        if [[ ${provider_worker_retired[$token]} != true && ! -e $FAKE_ROOT/ipc/$token/ready ]]; then
            printf '%s\n' "$token" > "$FAKE_ROOT/ipc/$token/ready"
            if [[ ${provider_worker_roles[$token]} == prediction ]]; then
                printf '{"runtime":{}}\n' > "$FAKE_ROOT/ipc/$token/metadata.json"
            fi
            printf '{"allocation":"%s"}\n' "$token" > "$FAKE_ROOT/owned/$token/diagnostics/resources.json"
        fi
    done
    if [[ -e $FAKE_ROOT/done ]]; then
        [[ ${#provider_worker_roles[@]} == 0 ]]
        provider_workers_close
        exit 0
    fi
    sleep 0.01
done
exit 97
"""
        owner = asyncio.create_task(asyncio.to_thread(host.run, program))
        result = None
        try:
            root = host.allocation_root
            async with asyncio.timeout(2):
                while not (root / "requests").exists():
                    if owner.done():
                        self.fail(owner.result().stderr)
                    await asyncio.sleep(0.005)
            for index, role in enumerate(("prediction", "reader", "smiles", "prediction", "reader"), 1):
                token = f"{index:032x}"
                allocation = WorkerAllocation(
                    role=role,
                    allocation=token,
                    request_root=root / "requests",
                    receipt_root=root / "receipts",
                    ipc_root=root / "ipc",
                    operation_seconds=2,
                )
                await allocation.start()
                worker = Mock(close=Mock(), wait_closed=AsyncMock())
                failures = await allocation.retire_channel(worker)
                self.assertEqual(failures, [], owner.result().stderr if owner.done() else "Host observation is still running")
                self.assertEqual(list((root / "requests").iterdir()), [])
                self.assertEqual(
                    (root / "history" / token / "requests" / "consumed").read_bytes(), f"consumed {token}\n".encode()
                )
                self.assertEqual((root / "receipts" / token / "archived").read_bytes(), f"archived {token}\n".encode())
                worker.close.assert_called_once()
                worker.wait_closed.assert_awaited_once()
            # All original archive/retirement assertions above must pass before
            # the real Python handoff asks this same host to release its leaves.
            self.assertEqual(
                await asyncio.to_thread(
                    release_native_history,
                    frontier=5,
                    previous=0,
                    request_root=root / "requests",
                    receipt_root=root / "receipts",
                    seconds=2,
                ),
                5,
            )
            self.assertEqual(list((root / "history").iterdir()), [])
            self.assertEqual([path.name for path in (root / "receipts").iterdir()], [".history-released"])
        finally:
            if host.allocation_root.exists():
                (host.allocation_root / "done").touch(mode=0o600)
            result = await asyncio.shield(owner)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([row[0] for row in host.commands()].count(b"create"), 5)
        self.assertEqual([row[0] for row in host.commands()].count(b"rm"), 5)

    async def test_consumption_follows_original_setup_retirement_and_channel_close_then_accepts_one_archive_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "requests").mkdir()
            (root / "receipts").mkdir()
            allocation = WorkerAllocation(
                role="reader",
                allocation=TOKEN,
                request_root=root / "requests",
                receipt_root=root / "receipts",
                ipc_root=root / "ipc",
                operation_seconds=1,
            )
            closed, release = asyncio.Event(), asyncio.Event()

            async def wait_closed():
                closed.set()
                await release.wait()

            worker = Mock(close=Mock(), wait_closed=AsyncMock(side_effect=wait_closed))
            start = asyncio.create_task(allocation.start())
            retirement = None
            try:
                async with asyncio.timeout(3):
                    request = root / "requests" / TOKEN
                    while not (request / "allocate").exists():
                        await asyncio.sleep(0.01)
                    receipt = root / "receipts" / TOKEN
                    receipt.mkdir()
                    ipc = root / "ipc" / TOKEN
                    ipc.mkdir(parents=True)
                    (ipc / "ready").write_bytes((TOKEN + "\n").encode())
                    (receipt / "allocated").write_bytes(f"allocated reader {TOKEN}\n".encode())
                    await start
                    retirement = asyncio.create_task(allocation.retire_channel(worker))
                    while not (request / "retire").exists():
                        await asyncio.sleep(0.01)
                    self.assertFalse(closed.is_set())
                    self.assertFalse((request / "consumed").exists())
                    (receipt / "result").write_bytes(f"retired {TOKEN}\n".encode())
                    await closed.wait()
                    self.assertTrue(allocation.operation.done())
                    self.assertTrue(allocation._publication.done())
                    self.assertFalse((request / "consumed").exists())
                    release.set()
                    while not (request / "consumed").exists():
                        await asyncio.sleep(0.01)
                    self.assertEqual((request / "consumed").read_bytes(), f"consumed {TOKEN}\n".encode())
                    self.assertFalse(retirement.done())
                    # The host can rename the whole request tree immediately;
                    # publication must have no subsequent pathname cleanup.
                    request.rename(root / "preserved-request")
                    (receipt / "archived").write_bytes(f"archived {TOKEN}\n".encode())
                    self.assertEqual(await retirement, [])
                    self.assertEqual(await allocation.retire_channel(worker), [])
                    worker.close.assert_called_once()
                    worker.wait_closed.assert_awaited_once()
                    self.assertEqual((root / "preserved-request" / "consumed").read_bytes(), f"consumed {TOKEN}\n".encode())
            finally:
                release.set()
                for task in (start, retirement):
                    if task is not None and not task.done():
                        task.cancel()
                await asyncio.gather(*(task for task in (start, retirement) if task is not None), return_exceptions=True)

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
    def test_closed_history_release_removes_only_known_native_leaves_and_keeps_sequence_frontier(self):
        host = HostFixture()
        self.addCleanup(host.close)
        result = host.run(r"""
umask 077
provider_workers_open "$FAKE_ROOT" 3 1
for number in 1 2; do
    printf -v token '%032x' "$number"
    mkdir -m 700 -- "$FAKE_ROOT/requests/$token"
    printf 'allocate reader %s\n' "$token" > "$FAKE_ROOT/requests/$token/allocate"
    provider_workers_service
    provider_workers_retire "$token"
    printf 'consumed %s\n' "$token" > "$FAKE_ROOT/requests/$token/consumed"
    provider_workers_service
    printf 'release %s\n' "$token" > "$FAKE_ROOT/requests/.release-history"
    provider_workers_service
    cmp -s -- "$FAKE_ROOT/receipts/.history-released" <(printf 'released %s\n' "$token")
    [[ ! -e $FAKE_ROOT/history/$token && ! -e $FAKE_ROOT/receipts/$token ]]
    [[ $provider_workers_accepted_number == "$number" ]]
    provider_workers_service
done
mkdir -m 700 -- "$FAKE_ROOT/requests/$FAKE_TOKEN"
printf 'retire %s\n' "$FAKE_TOKEN" > "$FAKE_ROOT/requests/$FAKE_TOKEN/retire"
if provider_workers_service; then exit 97; fi
[[ $provider_workers_diagnostic == *'sequence'* ]]
provider_workers_close
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([row[0] for row in host.commands()].count(b"create"), 2)
        self.assertEqual(list((host.allocation_root / "history").iterdir()), [])

    def test_consumed_retirements_reuse_active_capacity_and_preserve_every_original_tree(self):
        host = HostFixture()
        self.addCleanup(host.close)
        result = host.run(r"""
umask 077
provider_workers_open "$FAKE_ROOT" 3 1
for index in {1..8}; do
    printf -v token '%032x' "$index"
    case $((index % 3)) in 0) role=prediction ;; 1) role=reader ;; 2) role=smiles ;; esac
    mkdir -m 700 -- "$FAKE_ROOT/requests/$token"
    printf 'allocate %s %s\n' "$role" "$token" > "$FAKE_ROOT/requests/$token/allocate"
    provider_workers_service
    printf '{"allocation":"%s"}\n' "$token" > "$FAKE_ROOT/owned/$token/diagnostics/resources.json"
    provider_workers_retire "$token"
    # Physical removal alone must not renew the request slot.
    [[ -d $FAKE_ROOT/requests/$token && ${provider_worker_retired[$token]} == true ]]
    printf 'consumed %s\n' "$token" > "$FAKE_ROOT/requests/$token/consumed"
    provider_workers_service
    [[ ! -e $FAKE_ROOT/requests/$token && ! -e $FAKE_ROOT/owned/$token ]]
    [[ ${#provider_worker_roles[@]} == 0 && ${#provider_worker_ids[@]} == 0 && ${#provider_worker_retired[@]} == 0 ]]
    cmp -s -- "$FAKE_ROOT/receipts/$token/archived" <(printf 'archived %s\n' "$token")
done
provider_workers_close
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([row[0] for row in host.commands()].count(b"create"), 8)
        self.assertEqual([row[0] for row in host.commands()].count(b"rm"), 8)
        root = host.allocation_root
        self.assertEqual(list((root / "requests").iterdir()), [])
        self.assertEqual(list((root / "owned").iterdir()), [])
        self.assertEqual(len(list((root / "history").iterdir())), 8)
        for index in range(1, 9):
            token = f"{index:032x}"
            role = ("prediction", "reader", "smiles")[index % 3]
            history = root / "history" / token
            self.assertEqual((history / "requests" / "allocate").read_bytes(), f"allocate {role} {token}\n".encode())
            self.assertEqual((history / "requests" / "consumed").read_bytes(), f"consumed {token}\n".encode())
            self.assertFalse((history / "owned" / "diagnostics" / "stderr").exists())
            self.assertFalse((history / "owned" / "diagnostics" / "stdout").exists())
            report = history / "owned" / "diagnostics" / "resources.json"
            self.assertEqual(report.read_bytes(), (root / "receipts" / token / "resources.json").read_bytes())
            self.assertEqual(report.stat().st_ino, (root / "receipts" / token / "resources.json").stat().st_ino)
            self.assertTrue((history / "owned" / "create-intent").exists())
            self.assertTrue((history / "owned" / "retirement" / "remove.stdout").exists())
        for path in (root / "history", *(root / "history").rglob("*")):
            self.assertEqual(path.stat().st_mode & 0o777, 0o700 if path.is_dir() else 0o600)

    def test_retired_never_dispatched_request_can_be_consumed_without_a_worker_tree(self):
        for timing in ("retire_only", "before_admission", "during_admission"):
            with self.subTest(timing=timing):
                host = HostFixture()
                self.addCleanup(host.close)
                host.environment["FAKE_RETIREMENT_TIMING"] = timing
                result = host.run(r"""
umask 077
provider_workers_open "$FAKE_ROOT" 3 1
mkdir -m 700 -- "$FAKE_ROOT/requests/$FAKE_TOKEN"
if [[ $FAKE_RETIREMENT_TIMING != retire_only ]]; then
    printf 'allocate reader %s\n' "$FAKE_TOKEN" > "$FAKE_ROOT/requests/$FAKE_TOKEN/allocate"
fi
if [[ $FAKE_RETIREMENT_TIMING == during_admission ]]; then
    provider_workers_retirement_requested() {
        local request=$provider_workers_root/requests/$1/retire
        if [[ ! -e $request ]]; then
            printf 'retire %s\n' "$1" > "$request"
            return 1
        fi
        cmp -s -- "$request" <(printf 'retire %s\n' "$1")
    }
else
    printf 'retire %s\n' "$FAKE_TOKEN" > "$FAKE_ROOT/requests/$FAKE_TOKEN/retire"
fi
provider_workers_service
[[ -z ${provider_worker_roles[$FAKE_TOKEN]} && -z ${provider_worker_ids[$FAKE_TOKEN]} ]]
printf 'consumed %s\n' "$FAKE_TOKEN" > "$FAKE_ROOT/requests/$FAKE_TOKEN/consumed"
provider_workers_service
[[ ! -e $FAKE_ROOT/owned/$FAKE_TOKEN && ! -e $FAKE_ROOT/requests/$FAKE_TOKEN ]]
[[ -d $FAKE_ROOT/history/$FAKE_TOKEN/requests && ! -e $FAKE_ROOT/history/$FAKE_TOKEN/owned ]]
cmp -s -- "$FAKE_ROOT/receipts/$FAKE_TOKEN/archived" <(printf 'archived %s\n' "$FAKE_TOKEN")
provider_workers_close
""")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse((host.root / "commands").exists())

    def test_created_container_admission_accepts_only_matching_complete_private_receipts(self):
        for suffix in ("", "\n"):
            with self.subTest(cid_suffix=suffix):
                host = HostFixture()
                self.addCleanup(host.close)
                for name, content in (("worker.cid", CID + suffix), ("create.stdout", CID + "\n")):
                    path = host.root / name
                    path.write_text(content)
                    path.chmod(0o600)
                result = host.run(r"""
worker_container_created "$FAKE_STATE"
[[ $worker_created_id == "$FAKE_CID" ]]
""")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse((host.root / "commands").exists())

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
unallocated=00000000000000000000000000000002
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
