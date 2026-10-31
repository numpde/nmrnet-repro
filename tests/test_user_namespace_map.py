"""Selected Engine ID maps preserve host ownership for deployment helpers."""

import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class UserNamespaceMapTests(unittest.TestCase):
    def resolve(self, content, host_id):
        with tempfile.TemporaryDirectory() as directory:
            mapping = Path(directory) / "uid_map"
            mapping.write_text(content)
            return subprocess.run(
                ["bash", "-c", 'source "$1"; container_id_for_host_id "$2" "$3"',
                 "bash", str(ROOT / "tools/user_namespace_map.sh"), str(mapping), str(host_id)],
                capture_output=True, text=True, check=False,
            )

    def test_maps_host_caller_inside_rootless_subordinate_range(self):
        result = self.resolve("0 1042 1\n1 100000 65536\n", 100417)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "418\n")

    def test_maps_identity_range_for_rootful_engine(self):
        result = self.resolve("0 0 4294967295\n", 2001)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "2001\n")

    def test_refuses_unmapped_or_ambiguous_host_id(self):
        self.assertNotEqual(self.resolve("0 100000 65536\n", 999).returncode, 0)
        self.assertNotEqual(self.resolve("0 100000 65536\n2 100002 1\n", 100002).returncode, 0)

    def test_refuses_malformed_map_lines(self):
        self.assertNotEqual(self.resolve("0 100000 65536\ninvalid\n", 100001).returncode, 0)
        self.assertNotEqual(self.resolve("00 100000 65536\n", 100001).returncode, 0)

    def test_maps_container_service_and_root_ids_to_the_host_namespace(self):
        with tempfile.TemporaryDirectory() as directory:
            mapping = Path(directory) / "uid_map"
            mapping.write_text("0 1000 1\n1 100000 65536\n")
            result = subprocess.run(
                ["bash", "-c", 'source "$1"; host_id_for_container_id "$2" "$3"',
                 "bash", str(ROOT / "tools/user_namespace_map.sh"), str(mapping), "1000"],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "100999\n")

            root = subprocess.run(
                ["bash", "-c", 'source "$1"; host_id_for_container_id "$2" 0',
                 "bash", str(ROOT / "tools/user_namespace_map.sh"), str(mapping)],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(root.returncode, 0, root.stderr)
            self.assertEqual(root.stdout, "1000\n")


if __name__ == "__main__":
    unittest.main()
