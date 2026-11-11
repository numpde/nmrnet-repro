"""Selected Engine ID maps preserve host ownership for deployment helpers."""

import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path("/workspace")


class UserNamespaceMapTests(unittest.TestCase):
    def resolve(self, content, host_id):
        with tempfile.TemporaryDirectory() as directory:
            mapping = Path(directory) / "uid_map"
            mapping.write_text(content)
            return subprocess.run(
                [
                    "bash",
                    "-c",
                    'source "$1"; container_id_for_host_id "$2" "$3"',
                    "bash",
                    str(ROOT / "tools/user_namespace_map.sh"),
                    str(mapping),
                    str(host_id),
                ],
                capture_output=True,
                text=True,
                check=False,
            )

    def test_maps_host_caller_inside_rootless_subordinate_range(self):
        result = self.resolve("0 1042 1\n1 100000 65536\n", 100417)
        self.assertEqual(0, result.returncode, msg=result.stderr)
        self.assertEqual("418\n", result.stdout)

    def test_maps_identity_range_for_rootful_engine(self):
        result = self.resolve("0 0 4294967295\n", 2001)
        self.assertEqual(0, result.returncode, msg=result.stderr)
        self.assertEqual("2001\n", result.stdout)

    def test_refuses_unmapped_or_ambiguous_host_id(self):
        self.assertNotEqual(self.resolve("0 100000 65536\n", 999).returncode, 0)
        self.assertNotEqual(self.resolve("0 100000 65536\n2 100002 1\n", 100002).returncode, 0)

    def test_refuses_malformed_map_lines(self):
        self.assertNotEqual(self.resolve("0 100000 65536\ninvalid\n", 100001).returncode, 0)
        self.assertNotEqual(self.resolve("00 100000 65536\n", 100001).returncode, 0)

    def test_initial_namespace_identity_requires_nsfs_and_linux_initial_inode(self):
        script = (
            'source "$1"; '
            "linux_namespace_is_initial 6e736673 4026531837 4026531837 && "
            "! linux_namespace_is_initial 6e736673 4026531838 4026531837 && "
            "! linux_namespace_is_initial 65786563 4026531837 4026531837 && "
            "linux_namespace_is_initial 6e736673 4026531836 4026531836"
        )
        result = subprocess.run(
            ["bash", "-c", script, "bash", str(ROOT / "tools/user_namespace_map.sh")],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, msg=result.stderr)

    def test_probe_identity_requires_exact_token_and_stable_starttime(self):
        token = "nmrnet-probe-test-token"
        process = subprocess.Popen(["bash", "-c", f"exec -a {token} sleep 30"])
        try:
            script = (
                'source "$1"; '
                'start=$(probe_process_starttime "$2") && '
                'probe_process_has_token "$2" "$3" && '
                'probe_process_is_same "$2" "$3" "$start" && '
                '! probe_process_has_token "$2" wrong-token'
            )
            result = subprocess.run(
                ["bash", "-c", script, "bash", str(ROOT / "tools/user_namespace_map.sh"), str(process.pid), token],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(0, result.returncode, msg=result.stderr)
            starttime = subprocess.run(
                [
                    "bash",
                    "-c",
                    'source "$1"; probe_process_starttime "$2"',
                    "bash",
                    str(ROOT / "tools/user_namespace_map.sh"),
                    str(process.pid),
                ],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            still_running = subprocess.run(
                [
                    "bash",
                    "-c",
                    'source "$1"; probe_process_is_retired "$2" "$3"',
                    "bash",
                    str(ROOT / "tools/user_namespace_map.sh"),
                    str(process.pid),
                    starttime,
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(1, still_running.returncode)
        finally:
            process.terminate()
            process.wait(timeout=5)
        retired = subprocess.run(
            [
                "bash",
                "-c",
                'source "$1"; probe_process_is_retired "$2" "$3"',
                "bash",
                str(ROOT / "tools/user_namespace_map.sh"),
                str(process.pid),
                starttime,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, retired.returncode, msg=retired.stderr)

    def test_zombie_probe_process_counts_as_retired(self):
        process = subprocess.Popen(["bash", "-c", "exit 0"])
        try:
            starttime = subprocess.run(
                [
                    "bash",
                    "-c",
                    'source "$1"; probe_process_starttime "$2"',
                    "bash",
                    str(ROOT / "tools/user_namespace_map.sh"),
                    str(process.pid),
                ],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            process_state = ""
            for _ in range(100):
                stat_file = Path(f"/proc/{process.pid}/stat")
                if not stat_file.exists():
                    break
                process_state = stat_file.read_text().rpartition(") ")[2].split()[0]
                if process_state == "Z":
                    break
                time.sleep(0.01)
            self.assertEqual(process_state, "Z")
            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    'source "$1"; probe_process_is_retired "$2" "$3"',
                    "bash",
                    str(ROOT / "tools/user_namespace_map.sh"),
                    str(process.pid),
                    starttime,
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(0, result.returncode, msg=result.stderr)
        finally:
            process.wait(timeout=5)

    def test_maps_container_service_and_root_ids_to_the_host_namespace(self):
        with tempfile.TemporaryDirectory() as directory:
            mapping = Path(directory) / "uid_map"
            mapping.write_text("0 1000 1\n1 100000 65536\n")
            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    'source "$1"; host_id_for_container_id "$2" "$3"',
                    "bash",
                    str(ROOT / "tools/user_namespace_map.sh"),
                    str(mapping),
                    "1000",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(0, result.returncode, msg=result.stderr)
            self.assertEqual("100999\n", result.stdout)

            root = subprocess.run(
                [
                    "bash",
                    "-c",
                    'source "$1"; host_id_for_container_id "$2" 0',
                    "bash",
                    str(ROOT / "tools/user_namespace_map.sh"),
                    str(mapping),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(0, root.returncode, msg=root.stderr)
            self.assertEqual("1000\n", root.stdout)


if __name__ == "__main__":
    unittest.main()
