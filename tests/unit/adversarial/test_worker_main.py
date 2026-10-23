"""Invalid runner profiles fail before child creation or scientific dispatch."""

from tools.container_guard import require_container

require_container()

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from nmrnet_provider import native_child, worker_main
from tests.unit.normative.test_worker_main import COMMON


class RunnerProfileRefusalTests(unittest.TestCase):
    def test_invalid_budget_and_wrong_role_settings_refuse_before_supervision(self):
        for arguments in (
            ["reader", *COMMON, "--operation-seconds", "nan"],
            ["reader", *COMMON, "--io-seconds", "0"],
            ["reader", *COMMON, "--seed", "1"],
            ["smiles", *COMMON],
        ):
            with self.subTest(arguments=arguments), patch.object(worker_main, "supervise") as supervise:
                with self.assertRaises((SystemExit, ValueError)):
                    worker_main.main(arguments)
                supervise.assert_not_called()

    def test_changed_embedded_declaration_refuses_before_release_composition(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for leaf in ("input", "embedded"):
                (root / leaf).mkdir()
                (root / leaf / "inference.lock").write_text(leaf)
            with (
                patch.object(native_child, "INPUT_ROOT", root / "input"),
                patch.object(native_child, "INFERENCE_ROOT", root / "embedded"),
                patch("nmrnet_inference.worker.serve_released_connection") as serve,
                self.assertRaisesRegex(ValueError, "embedded inference.lock"),
            ):
                native_child._prediction(SimpleNamespace(operation="a" * 32, io_seconds=2), object())
            serve.assert_not_called()
