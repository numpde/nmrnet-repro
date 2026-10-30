"""Disposable shared fixture retirement admits only its original resources."""

from tools.container_guard import require_container

require_container()

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import unittest
import uuid


CHECKER = r'''#!/usr/bin/env bash
checker_begin() {
    operation_directory=$TEST_ROOT/operation
    mkdir -p "$operation_directory"
    operation_diagnostic= docker_client_directory=$TEST_ROOT/client
    engine=(fake)
    trap checker_finish EXIT
}
checker_finish() {
    local status=$?
    trap - EXIT
    rm -f "$TEST_ROOT/reservation"
    exit "$status"
}
checker_create() { printf 'reserved\n' > "$TEST_ROOT/reservation"; }
checker_metadata() {
    local destination=$1; shift
    local id volume owner kind settings
    case "$1 $2" in
        'image inspect') printf '%s delivery %064d\n' "$image" 1 > "$destination" ;;
        'volume ls') : > "$destination"; [[ ${COLLISION-} != volume ]] || printf '%s\n' "$project-queue" > "$destination" ;;
        'volume inspect')
            volume=${!#}; owner=$project; [[ $volume != "$proxy_volume" ]] || owner=$proxy_project
            [[ -f $TEST_ROOT/$volume ]] || return 1
            printf '%s|%s|%s|local|null|%s\n' "$volume" "$owner" "$volume" "${CREATED-chosen}" > "$destination" ;;
        'inspect --type')
            id=${!#}; kind=proxy; settings=$scratch/data/shared-settings
            [[ $id != "$INIT_ID" ]] || { kind=init; settings=; }
            [[ -f $TEST_ROOT/$kind ]] || return 1
            if [[ $* == *'.State.Status'* ]]; then printf 'exited|false|0|false\n' > "$destination"
            else printf '%s|%s|%s|%s|%s|%s|%s\n' "$id" "$image" "$proxy_project" "$proxy_project" "$kind" "$proxy_volume" "$settings" > "$destination"; fi ;;
        'ps --all')
            : > "$destination"
            case "$*" in
                *"volume=$proxy_volume"*)
                    [[ ! -f $TEST_ROOT/proxy ]] || printf '%s\n' "$PROXY_ID" >> "$destination"
                    [[ ! -f $TEST_ROOT/init ]] || printf '%s\n' "$INIT_ID" >> "$destination"
                    [[ ${FOREIGN-} != yes ]] || printf '%064d\n' 3 >> "$destination" ;;
                *"volume=$intake_volume"*) : ;;
                *"project=$proxy_project"*)
                    [[ ! -f $TEST_ROOT/proxy ]] || printf '%s\n' "$PROXY_ID" >> "$destination"
                    [[ ! -f $TEST_ROOT/init ]] || printf '%s\n' "$INIT_ID" >> "$destination" ;;
                *) [[ ${COLLISION-} != project ]] || printf '%064d\n' 4 > "$destination" ;;
            esac ;;
        *) printf 'Unexpected metadata: %s\n' "$*" >&2; return 1 ;;
    esac
}
checker_call() {
    local description=$1; shift 3
    [[ $1 == fake ]] || return 1
    shift
    printf '%s\n' "$*" >> "$TEST_ROOT/mutations"
    [[ ${FAIL_OPERATION-} != "$description" ]] || return 1
    case "$1" in
        stop) ;;
        rm) [[ ${!#} != "$PROXY_ID" ]] || rm "$TEST_ROOT/proxy"; [[ ${!#} != "$INIT_ID" ]] || rm "$TEST_ROOT/init" ;;
        volume) rm "$TEST_ROOT/${!#}" ;;
        *) return 1 ;;
    esac
}
'''


class DisposableSharedFixtureTests(unittest.TestCase):
    def setUp(self):
        self.scratch = Path("/tmp") / ("nmrnet-compose-" + uuid.uuid4().hex[:8])
        self.scratch.mkdir(mode=0o700)
        self.addCleanup(shutil.rmtree, self.scratch)
        self.project = "nmrnet-qualification-" + hashlib.sha256(str(self.scratch).encode()).hexdigest()[:24]
        self.image = "sha256:" + "a" * 64
        self.root = self.scratch / "test-owner"
        (self.root / "tools").mkdir(parents=True)
        (self.root / "tools/checker_operation.sh").write_text(CHECKER)
        (self.scratch / "delivery").mkdir(mode=0o700)
        self.env = os.environ | {
            "TEST_ROOT": str(self.root),
            "PROXY_ID": "1" * 64,
            "INIT_ID": "2" * 64,
        }

    def invoke(self, action, **changes):
        return subprocess.run(
            ["bash", "/workspace/tools/compose_fixture_shared.sh", action, str(self.scratch),
             self.project, self.image, str(self.root)],
            env=self.env | changes, text=True, capture_output=True, check=False,
        )

    def start(self):
        for name in ("proxy", "init", self.project + "-proxy-socket", self.project + "-outbox-socket", self.project + "-queue"):
            (self.root / name).touch()
        result = self.invoke("capture")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_preflight_rejects_existing_project_or_volume(self):
        for collision in ("project", "volume"):
            with self.subTest(collision=collision):
                self.assertNotEqual(self.invoke("preflight", COLLISION=collision).returncode, 0)
                self.assertFalse((self.root / "mutations").exists())
        self.assertEqual(self.invoke("preflight").returncode, 0)

    def test_retirement_removes_only_original_fixture_sockets_and_containers(self):
        self.start()
        result = self.invoke("retire")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / (self.project + "-queue")).exists())
        self.assertFalse((self.root / (self.project + "-proxy-socket")).exists())
        self.assertFalse((self.root / (self.project + "-outbox-socket")).exists())
        self.assertFalse((self.root / "reservation").exists())
        self.assertNotIn("--volumes", (self.root / "mutations").read_text())

    def test_changed_volume_or_foreign_borrower_prevents_mutation(self):
        self.start()
        for change in ({"CREATED": "replacement"}, {"FOREIGN": "yes"}):
            with self.subTest(change=change):
                self.assertNotEqual(self.invoke("retire", **change).returncode, 0)
                self.assertFalse((self.root / "mutations").exists())

    def test_uncertain_mutation_retains_reservation_and_stops(self):
        self.start()
        result = self.invoke("retire", FAIL_OPERATION="stop disposable proxy container")
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.root / "reservation").exists())
        self.assertEqual(len((self.root / "mutations").read_text().splitlines()), 1)
        self.assertTrue((self.root / (self.project + "-queue")).exists())
