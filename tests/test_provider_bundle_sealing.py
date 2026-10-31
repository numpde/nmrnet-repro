"""Rootless staging keeps file permissions usable after ownership transfer."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.provider_deployment import seal_deployment_bundle


class ProviderBundleSealingTests(unittest.TestCase):
    def test_rootless_sealing_sets_file_modes_before_transferring_ownership(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory) / "generation"
            bundle.mkdir(mode=0o700)
            (bundle / "config").mkdir(mode=0o700)
            (bundle / "secrets/provider").mkdir(parents=True, mode=0o700)
            (bundle / "compose.json").write_text("{}")
            (bundle / "secrets/provider/credential").write_text("private")
            operations = []
            chmod = os.chmod

            def record_chmod(path, mode, **options):
                operations.append(("chmod", Path(path).relative_to(bundle).as_posix(), mode))
                return chmod(path, mode, **options)

            def record_chown(path, uid, gid, **options):
                operations.append(("chown", Path(path).relative_to(bundle).as_posix(), uid, gid))

            with patch("tools.provider_deployment.os.geteuid", side_effect=(os.getuid(), 0)), \
                    patch("tools.provider_deployment.os.chmod", side_effect=record_chmod), \
                    patch("tools.provider_deployment.os.chown", side_effect=record_chown):
                seal_deployment_bundle(bundle, 1234, 1234)
            for relative in ("compose.json", "secrets/provider/credential"):
                file_operations = [operation[0] for operation in operations if operation[1] == relative]
                self.assertEqual(file_operations, ["chmod", "chown"])
            self.assertEqual((bundle / "compose.json").stat().st_mode & 0o777, 0o444)
            self.assertEqual((bundle / "secrets/provider/credential").stat().st_mode & 0o777, 0o400)
