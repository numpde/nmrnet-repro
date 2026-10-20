"""Uncertain allocation remains owned; cancellation never repeats host effects."""

from tools.container_guard import require_container

require_container()

import asyncio
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.unit.worker_host_fakes import TOKEN as HOST_TOKEN
from tests.unit.worker_host_fakes import HostFixture

from nmrnet_provider.allocation import AllocationUnconfirmed, WorkerAllocation

TOKEN = "e" * 32


class WorkerAllocationAdversarialTests(unittest.IsolatedAsyncioTestCase):
    def allocation(self, root, **changes):
        settings = {
            "role": "prediction",
            "allocation": TOKEN,
            "request_root": root / "requests",
            "receipt_root": root / "receipts",
            "ipc_root": root / "ipc",
            "operation_seconds": 0.2,
        }
        settings.update(changes)
        return WorkerAllocation(**settings)

    async def wait_request(self, root):
        async with asyncio.timeout(1):
            while not (root / "requests" / TOKEN / "allocate").exists():
                await asyncio.sleep(0.01)

    async def test_cancelled_wait_retains_original_request_until_late_host_retirement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "requests").mkdir()
            (root / "receipts" / TOKEN).mkdir(parents=True)
            allocation = self.allocation(root)
            operation = asyncio.create_task(allocation.start())
            await self.wait_request(root)
            operation.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await operation
            self.assertFalse(allocation.operation.done())
            retirement = asyncio.create_task(allocation.retirement.retire())
            async with asyncio.timeout(1):
                while not (root / "requests" / TOKEN / "retire").exists():
                    await asyncio.sleep(0.01)
                (root / "receipts" / TOKEN / "result").write_bytes(f"retired {TOKEN}\n".encode())
                await retirement
                await allocation.settle()
            with self.assertRaisesRegex(AllocationUnconfirmed, "retir.*adopt"):
                await allocation.start()
            self.assertEqual(len(list((root / "requests").iterdir())), 1)

    async def test_wrong_or_oversized_host_receipt_is_never_admitted(self):
        for raw in (f"allocated reader {TOKEN}\n".encode(), b"allocated prediction " + b"e" * 1000):
            with self.subTest(raw=raw[:30]), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "requests").mkdir()
                (root / "receipts" / TOKEN).mkdir(parents=True)
                (root / "receipts" / TOKEN / "allocated").write_bytes(raw)
                allocation = self.allocation(root)
                with self.assertRaisesRegex(AllocationUnconfirmed, "receipt"):
                    await allocation.start()
                with self.assertRaises(AllocationUnconfirmed):
                    await allocation.start()
                self.assertEqual(len(list((root / "requests").iterdir())), 1)

    def test_role_token_and_deadline_admission_have_no_filesystem_effects(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for changes in ({"role": "command"}, {"allocation": "../foreign"}, {"operation_seconds": 0}):
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    self.allocation(root, **changes)
                self.assertEqual(list(root.iterdir()), [])

    async def test_cancelled_before_publication_retires_only_after_its_own_leaf_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "requests").mkdir()
            (root / "receipts" / TOKEN).mkdir(parents=True)
            allocation = self.allocation(root, operation_seconds=1)
            entered, release = threading.Event(), threading.Event()
            publish = allocation._publish

            def held_publication():
                entered.set()
                release.wait(2)
                publish()

            with patch.object(allocation, "_publish", side_effect=held_publication):
                start = asyncio.create_task(allocation.start())
                retirement = None
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                    start.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await start
                    retirement = asyncio.create_task(allocation.retirement.retire())
                    await asyncio.sleep(0)
                    self.assertFalse(retirement.done())
                    self.assertFalse((root / "requests" / TOKEN).exists())
                    release.set()
                    async with asyncio.timeout(2):
                        while not (root / "requests" / TOKEN / "retire").exists():
                            await asyncio.sleep(0.01)
                        (root / "receipts" / TOKEN / "result").write_bytes(f"retired {TOKEN}\n".encode())
                        await retirement
                        await allocation.settle()
                finally:
                    release.set()
                    await asyncio.gather(start, *([] if retirement is None else [retirement]), return_exceptions=True)

    async def test_existing_token_leaf_never_becomes_this_requests_retirement_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            leaf = root / "requests" / TOKEN
            leaf.mkdir(parents=True)
            (leaf / "allocate").write_bytes(b"original-evidence")
            allocation = self.allocation(root)
            with self.assertRaises(AllocationUnconfirmed):
                await allocation.start()
            with self.assertRaises(AllocationUnconfirmed):
                await allocation.retirement.retire()
            self.assertEqual(list(leaf.iterdir()), [leaf / "allocate"])
            self.assertEqual((leaf / "allocate").read_bytes(), b"original-evidence")

    async def test_allocation_becomes_visible_only_after_its_complete_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "requests").mkdir()
            (root / "receipts" / TOKEN).mkdir(parents=True)
            allocation = self.allocation(root, operation_seconds=1)
            entered, release = threading.Event(), threading.Event()
            link = os.link

            def held_commit(*arguments, **keywords):
                entered.set()
                release.wait(2)
                return link(*arguments, **keywords)

            with patch("nmrnet_provider.allocation.os.link", side_effect=held_commit):
                operation = asyncio.create_task(allocation.start())
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                    leaf = root / "requests" / TOKEN
                    self.assertFalse((leaf / "allocate").exists())
                    self.assertEqual((leaf / "pending").read_bytes(), f"allocate prediction {TOKEN}\n".encode())
                    (root / "ipc" / TOKEN).mkdir(parents=True)
                    (root / "ipc" / TOKEN / "ready").write_bytes((TOKEN + "\n").encode())
                    (root / "ipc" / TOKEN / "metadata.json").write_bytes(b'{"runtime":{}}')
                    (root / "receipts" / TOKEN / "allocated").write_bytes(f"allocated prediction {TOKEN}\n".encode())
                    release.set()
                    await operation
                finally:
                    release.set()
                    await asyncio.gather(operation, return_exceptions=True)

    async def test_late_retirement_receipt_can_be_observed_without_another_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "requests").mkdir()
            receipt = root / "receipts" / TOKEN
            receipt.mkdir(parents=True)
            ipc = root / "ipc" / TOKEN
            ipc.mkdir(parents=True)
            (ipc / "ready").write_bytes((TOKEN + "\n").encode())
            (ipc / "metadata.json").write_bytes(b'{"runtime":{}}')
            (receipt / "allocated").write_bytes(f"allocated prediction {TOKEN}\n".encode())
            allocation = self.allocation(root, operation_seconds=0.05)
            await allocation.start()
            with self.assertRaises(AllocationUnconfirmed):
                await allocation.retire()
            async with asyncio.timeout(1):
                while not allocation._retirement.done():
                    await asyncio.sleep(0.01)
            original_failure = allocation.last_failure
            request = root / "requests" / TOKEN / "retire"
            original_inode = request.stat().st_ino
            (receipt / "result").write_bytes(f"retired {TOKEN}\n".encode())
            await allocation.retire()
            self.assertIs(allocation.last_failure, original_failure)
            self.assertEqual(request.stat().st_ino, original_inode)
            self.assertEqual(request.read_bytes(), f"retire {TOKEN}\n".encode())
            with self.assertRaises(AllocationUnconfirmed):
                await allocation.start()


class HostAllocationAdversarialTests(unittest.TestCase):
    def test_create_finishing_after_drain_deadline_is_retired_without_start(self):
        host = self.host()
        result = host.run(r"""
provider_workers_open "$FAKE_ROOT" 3 1
provider_workers_creation_deadline=$((SECONDS + 1))
provider_workers_command() {
    local operation=$1 directory=$2
    shift 2
    worker_retirement_command "$operation" "$directory" "${engine[@]}" "$@" || return $?
    if [[ $operation == create ]]; then SECONDS=$provider_workers_creation_deadline; fi
}
mkdir -m 700 -- "$FAKE_ROOT/requests/$FAKE_TOKEN"
printf 'allocate reader %s\n' "$FAKE_TOKEN" > "$FAKE_ROOT/requests/$FAKE_TOKEN/allocate"
provider_workers_service
[[ $provider_workers_admission == closed && ${provider_worker_retired[$FAKE_TOKEN]} == true ]]
provider_workers_close
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([row[0] for row in host.commands()], [b"create", b"stop", b"inspect", b"rm"])

    def test_drain_deadline_closes_creation_between_two_queued_requests(self):
        host = self.host()
        result = host.run(r"""
provider_workers_open "$FAKE_ROOT" 4 1
provider_workers_creation_deadline=$((SECONDS + 1))
provider_workers_create() { printf '%s\n' "$1" >> "$FAKE_STATE/dispatches"; SECONDS=$provider_workers_creation_deadline; }
first=11111111111111111111111111111111
second=22222222222222222222222222222222
for token in "$first" "$second"; do
    mkdir -m 700 -- "$FAKE_ROOT/requests/$token"
    printf 'allocate prediction %s\n' "$token" > "$FAKE_ROOT/requests/$token/allocate"
done
provider_workers_service
[[ $provider_workers_admission == closed && ! -e $FAKE_ROOT/receipts/$second ]]
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((host.root / "dispatches").read_text(), "prediction\n")
        self.assertFalse((host.root / "commands").exists())

    def test_created_container_refusal_clears_identity_without_engine_effects(self):
        for fault in ("missing", "mismatch", "trailing", "stdout_trailing", "linked", "public", "unretired"):
            with self.subTest(fault=fault):
                host = self.host()
                host.environment["FAKE_FAULT"] = fault
                result = host.run(r"""
umask 077
printf '%s\n' "$FAKE_CID" > "$FAKE_STATE/worker.cid"
printf '%s\n' "$FAKE_CID" > "$FAKE_STATE/create.stdout"
case "$FAKE_FAULT" in
    missing) rm -- "$FAKE_STATE/worker.cid" ;;
    mismatch) printf '%064d\n' 0 > "$FAKE_STATE/create.stdout" ;;
    trailing) printf '\n' >> "$FAKE_STATE/worker.cid" ;;
    stdout_trailing) printf '\n' >> "$FAKE_STATE/create.stdout" ;;
    linked) mv -- "$FAKE_STATE/create.stdout" "$FAKE_STATE/original"; ln -s original "$FAKE_STATE/create.stdout" ;;
    public) chmod 644 -- "$FAKE_STATE/create.stdout" ;;
    unretired) inference_retirement_confirmed=false ;;
esac
worker_created_id=stale
if worker_container_created "$FAKE_STATE"; then exit 97; fi
[[ -z $worker_created_id && -n $worker_retirement_diagnostic ]]
""")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse((host.root / "commands").exists())

    def test_closed_admission_services_retirement_without_starting_queued_work(self):
        host = HostFixture()
        self.addCleanup(host.close)
        result = host.run(r"""
provider_workers_open "$FAKE_ROOT" 3 1
provider_workers_admission=closed
mkdir -m 700 -- "$FAKE_ROOT/requests/$FAKE_TOKEN"
printf 'allocate reader %s\n' "$FAKE_TOKEN" > "$FAKE_ROOT/requests/$FAKE_TOKEN/allocate"
provider_workers_service
[[ ! -e $FAKE_ROOT/owned/$FAKE_TOKEN ]]
printf 'retire %s\n' "$FAKE_TOKEN" > "$FAKE_ROOT/requests/$FAKE_TOKEN/retire"
provider_workers_service
provider_workers_close
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((host.root / "commands").exists())
        self.assertEqual(
            (host.allocation_root / "receipts" / HOST_TOKEN / "result").read_bytes(), f"retired {HOST_TOKEN}\n".encode()
        )

    def test_prediction_reserves_its_source_calls_and_capacity_refusal_still_allows_retirement(self):
        for prior in (2, 3):
            with self.subTest(retained_requests=prior):
                host = HostFixture()
                self.addCleanup(host.close)
                host.environment["FAKE_PRIOR"] = str(prior)
                result = host.run(r"""
provider_workers_admission=open
provider_workers_open "$FAKE_ROOT" 6 2
for (( index=1; index<=FAKE_PRIOR; index++ )); do
    printf -v token '%032x' "$index"
    mkdir -m 700 -- "$FAKE_ROOT/requests/$token"
    printf 'retire %s\n' "$token" > "$FAKE_ROOT/requests/$token/retire"
done
provider_workers_service
mkdir -m 700 -- "$FAKE_ROOT/requests/$FAKE_TOKEN"
printf 'allocate prediction %s\n' "$FAKE_TOKEN" > "$FAKE_ROOT/requests/$FAKE_TOKEN/allocate"
provider_workers_service
if [[ $FAKE_PRIOR == 2 ]]; then
    [[ $provider_workers_admission == open ]]
    provider_workers_retire "$FAKE_TOKEN"
else
    [[ $provider_workers_admission == capacity-exhausted ]]
    [[ ! -e $FAKE_ROOT/owned/$FAKE_TOKEN && ! -e $FAKE_ROOT/receipts/$FAKE_TOKEN ]]
    printf 'retire %s\n' "$FAKE_TOKEN" > "$FAKE_ROOT/requests/$FAKE_TOKEN/retire"
    provider_workers_service
fi
provider_workers_close
""")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(len(list((host.allocation_root / "requests").iterdir())), prior + 1)
                if prior == 2:
                    self.assertEqual(
                        [row[0] for row in host.commands()], [b"create", b"start", b"inspect", b"stop", b"inspect", b"rm"]
                    )
                else:
                    self.assertFalse((host.root / "commands").exists())
                    self.assertEqual(
                        (host.allocation_root / "receipts" / HOST_TOKEN / "result").read_bytes(),
                        f"retired {HOST_TOKEN}\n".encode(),
                    )

    def test_full_source_budget_leaves_one_refused_prediction_retirable_within_the_bound(self):
        host = self.host()
        result = host.run(r"""
provider_workers_open "$FAKE_ROOT" 4 2
mkdir -m 700 -- "$FAKE_ROOT/requests/$FAKE_TOKEN"
printf 'allocate prediction %s\n' "$FAKE_TOKEN" > "$FAKE_ROOT/requests/$FAKE_TOKEN/allocate"
provider_workers_service
for index in 1 2; do
    printf -v token '%032x' "$index"
    mkdir -m 700 -- "$FAKE_ROOT/requests/$token"
    printf 'allocate reader %s\n' "$token" > "$FAKE_ROOT/requests/$token/allocate"
    provider_workers_service
    provider_workers_retire "$token"
done
provider_workers_retire "$FAKE_TOKEN"
next=ffffffffffffffffffffffffffffffff
mkdir -m 700 -- "$FAKE_ROOT/requests/$next"
printf 'allocate prediction %s\n' "$next" > "$FAKE_ROOT/requests/$next/allocate"
provider_workers_service
[[ $provider_workers_admission == capacity-exhausted && ! -e $FAKE_ROOT/owned/$next ]]
printf 'retire %s\n' "$next" > "$FAKE_ROOT/requests/$next/retire"
provider_workers_service
extra=eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee
mkdir -m 700 -- "$FAKE_ROOT/requests/$extra"
printf 'allocate reader %s\n' "$extra" > "$FAKE_ROOT/requests/$extra/allocate"
if provider_workers_service; then exit 97; fi
provider_workers_close
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([row[0] for row in host.commands()].count(b"create"), 3)
        refused = "f" * 32
        self.assertEqual((host.allocation_root / "receipts" / refused / "result").read_bytes(), f"retired {refused}\n".encode())
        self.assertFalse((host.allocation_root / "owned" / ("e" * 32)).exists())
        self.assertEqual(len(list((host.allocation_root / "requests").iterdir())), 5)

    def test_insufficient_initial_capacity_refuses_before_root_creation_or_engine_effects(self):
        for maximum, calls in ((2, 1), (3, 2), (3, 0), (3, "1+1")):
            with self.subTest(maximum=maximum, source_calls=calls):
                host = self.host()
                host.environment.update(FAKE_MAXIMUM=str(maximum), FAKE_CALLS=str(calls))
                result = host.run(r"""
if provider_workers_open "$FAKE_ROOT" "$FAKE_MAXIMUM" "$FAKE_CALLS"; then exit 97; fi
""")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(host.allocation_root.exists())
                self.assertFalse((host.root / "commands").exists())

    def host(self):
        host = HostFixture()
        self.addCleanup(host.close)
        return host

    def test_invalid_report_never_crosses_the_grant_or_holds_retirement(self):
        for kind in ("symlink", "oversized"):
            with self.subTest(kind=kind):
                host = self.host()
                host.environment["FAKE_REPORT_KIND"] = kind
                result = host.run("""
provider_workers_open "$FAKE_ROOT" 3 1
mkdir -- "$FAKE_ROOT/requests/$FAKE_TOKEN"
printf 'allocate reader %s\n' "$FAKE_TOKEN" > "$FAKE_ROOT/requests/$FAKE_TOKEN/allocate"
provider_workers_service
report=$FAKE_ROOT/owned/$FAKE_TOKEN/diagnostics/resources.json
if [[ $FAKE_REPORT_KIND == symlink ]]; then
    printf 'host-private-canary\n' > "$FAKE_STATE/private"
    ln -s -- "$FAKE_STATE/private" "$report"
else
    head -c 16385 /dev/zero > "$report"
fi
provider_workers_retire "$FAKE_TOKEN"
provider_workers_close
""")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse((host.allocation_root / "receipts" / HOST_TOKEN / "resources.json").exists())
                self.assertTrue((host.allocation_root / "receipts" / HOST_TOKEN / "result").exists())
                self.assertEqual(host.commands()[-1][0], b"rm")

    def test_failed_report_publication_does_not_repeat_removal_or_hold_retirement(self):
        host = self.host()
        result = host.run("""
provider_workers_open "$FAKE_ROOT" 3 1
mkdir -- "$FAKE_ROOT/requests/$FAKE_TOKEN"
printf 'allocate reader %s\n' "$FAKE_TOKEN" > "$FAKE_ROOT/requests/$FAKE_TOKEN/allocate"
provider_workers_service
printf '{}\n' > "$FAKE_ROOT/owned/$FAKE_TOKEN/diagnostics/resources.json"
ln() { return 93; }
if provider_workers_retire "$FAKE_TOKEN"; then exit 91; fi
[[ ${provider_worker_retired[$FAKE_TOKEN]} == true ]]
[[ $provider_workers_diagnostic == *'could not be retained'* ]]
provider_workers_retire "$FAKE_TOKEN"
provider_workers_close
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((host.allocation_root / "receipts" / HOST_TOKEN / "result").exists())
        self.assertEqual([row[0] for row in host.commands()].count(b"rm"), 1)

    def test_retirement_arriving_during_create_prevents_start_and_confirms_exact_cleanup(self):
        host = self.host()
        host.environment["FAKE_RETIRE_DURING_CREATE"] = "yes"
        result = host.run("""
provider_workers_open "$FAKE_ROOT" 3 1
mkdir -- "$FAKE_ROOT/requests/$FAKE_TOKEN"
printf 'allocate reader %s\n' "$FAKE_TOKEN" > "$FAKE_ROOT/requests/$FAKE_TOKEN/allocate"
provider_workers_service
provider_workers_close
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([row[0] for row in host.commands()], [b"create", b"stop", b"inspect", b"rm"])
        self.assertFalse((host.allocation_root / "receipts" / HOST_TOKEN / "allocated").exists())
        self.assertTrue((host.allocation_root / "receipts" / HOST_TOKEN / "result").exists())

    def test_mismatched_cid_holds_root_without_starting_or_deleting_by_guess(self):
        host = self.host()
        host.environment["FAKE_MISMATCH"] = "yes"
        result = host.run("""
provider_workers_open "$FAKE_ROOT" 3 1
mkdir -- "$FAKE_ROOT/requests/$FAKE_TOKEN"
printf 'allocate reader %s\n' "$FAKE_TOKEN" > "$FAKE_ROOT/requests/$FAKE_TOKEN/allocate"
if provider_workers_service; then exit 91; fi
if provider_workers_close; then exit 92; fi
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([row[0] for row in host.commands()], [b"create"])
        self.assertTrue((host.allocation_root / "owned" / HOST_TOKEN / "create-intent").exists())
        self.assertFalse((host.allocation_root / "receipts" / HOST_TOKEN / "allocated").exists())

    def test_retirement_before_create_has_no_engine_effects(self):
        host = self.host()
        result = host.run("""
provider_workers_open "$FAKE_ROOT" 3 1
mkdir -- "$FAKE_ROOT/requests/$FAKE_TOKEN"
printf 'allocate prediction %s\n' "$FAKE_TOKEN" > "$FAKE_ROOT/requests/$FAKE_TOKEN/allocate"
printf 'retire %s\n' "$FAKE_TOKEN" > "$FAKE_ROOT/requests/$FAKE_TOKEN/retire"
provider_workers_service
provider_workers_close
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((host.root / "commands").exists())

    def test_prior_root_evidence_refuses_restart_without_native_effects(self):
        host = self.host()
        host.allocation_root.mkdir(mode=0o700)
        (host.allocation_root / "unresolved-create").write_bytes(b"preserve")
        result = host.run("""
if provider_workers_open "$FAKE_ROOT" 3 1; then exit 91; fi
printf '%s' "$provider_workers_diagnostic"
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("prior evidence", result.stdout)
        self.assertFalse((host.root / "commands").exists())
        self.assertEqual((host.allocation_root / "unresolved-create").read_bytes(), b"preserve")

    def test_second_host_cannot_acquire_the_same_live_root(self):
        host = self.host()
        first = subprocess.Popen(
            ["bash", "-c", host.program('provider_workers_open "$FAKE_ROOT" 3 1; printf "ready\\n"; read -r release')],
            env=host.environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual(first.stdout.readline(), "ready\n")
            second = host.run(
                'if provider_workers_open "$FAKE_ROOT" 3 1; then exit 91; fi; printf "%s" "$provider_workers_diagnostic"'
            )
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertIn("another host owner", second.stdout)
        finally:
            stdout, stderr = first.communicate("release\n", timeout=3)
        self.assertEqual(first.returncode, 0, stdout + stderr)

    def test_pending_publication_is_ignored_until_the_complete_request_is_committed(self):
        host = self.host()
        result = host.run("""
provider_workers_open "$FAKE_ROOT" 3 1
mkdir -- "$FAKE_ROOT/requests/$FAKE_TOKEN"
printf 'allocate reader %s\n' "$FAKE_TOKEN" > "$FAKE_ROOT/requests/$FAKE_TOKEN/pending"
provider_workers_service
test ! -e "$FAKE_STATE/commands"
ln -- "$FAKE_ROOT/requests/$FAKE_TOKEN/pending" "$FAKE_ROOT/requests/$FAKE_TOKEN/allocate"
provider_workers_service
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([row[0] for row in host.commands()], [b"create", b"start", b"inspect"])

    def test_failed_owned_directory_or_intent_publication_never_dispatches_engine_create(self):
        for failure in ("directory", "intent"):
            with self.subTest(failure=failure):
                host = self.host()
                host.environment["FAILURE"] = failure
                result = host.run("""
provider_workers_open "$FAKE_ROOT" 3 1
mkdir -- "$FAKE_ROOT/requests/$FAKE_TOKEN"
printf 'allocate reader %s\n' "$FAKE_TOKEN" > "$FAKE_ROOT/requests/$FAKE_TOKEN/allocate"
mkdir() {
    if [[ $FAILURE == directory && "$*" == *"/owned/$FAKE_TOKEN"* ]]; then return 73; fi
    command mkdir "$@"
}
printf() {
    if [[ $FAILURE == intent && "$1" == '%s %s\\n' ]]; then return 74; fi
    command printf "$@"
}
if provider_workers_service; then exit 91; fi
test ! -e "$FAKE_STATE/commands"
if provider_workers_close; then exit 92; fi
""")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse((host.root / "commands").exists())

    def test_retirement_only_leaf_cannot_authorize_a_late_create(self):
        host = self.host()
        result = host.run("""
provider_workers_open "$FAKE_ROOT" 3 1
mkdir -- "$FAKE_ROOT/requests/$FAKE_TOKEN"
printf 'retire %s\n' "$FAKE_TOKEN" > "$FAKE_ROOT/requests/$FAKE_TOKEN/retire"
provider_workers_service
printf 'allocate prediction %s\n' "$FAKE_TOKEN" > "$FAKE_ROOT/requests/$FAKE_TOKEN/allocate"
provider_workers_service
provider_workers_close
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((host.root / "commands").exists())
        self.assertEqual(
            (host.allocation_root / "receipts" / HOST_TOKEN / "result").read_bytes(), f"retired {HOST_TOKEN}\n".encode()
        )
