"""Old global markers and mismatched case receipts cannot admit native work."""

import subprocess
import tempfile
import unittest
from pathlib import Path


class FormatHostAdmissionTests(unittest.TestCase):
    def test_format_selector_rejects_unknown_repeated_multiline_and_only_whitespace_names(self):
        for selection in ("../json", "json json", "json\nxyz", " ", "json;PRIVATE_CANARY"):
            with self.subTest(selection=selection):
                result = subprocess.run(
                    [
                        "bash",
                        "-euc",
                        'source "$1"; provider_service_select_formats "$2"',
                        "formats",
                        "/workspace/tools/provider_service_qualification.sh",
                        selection,
                    ],
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 2)
                self.assertNotIn(b"PRIVATE_CANARY", result.stdout + result.stderr)

    def test_missing_case_wrong_selection_and_linked_intent_refuse_with_owned_reason(self):
        token = "a" * 32
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            case = root / "formats" / "json"
            (case / "allocation-intents").mkdir(parents=True)
            # Prior global markers cannot replace this case's own admission.
            (root / "admitted").write_bytes(b"admitted\n")
            (root / "selected").write_bytes(b"sha256:" + b"0" * 64 + b"\n")
            (root / "active-case").write_bytes(b"json\n")
            (case / "selected").write_bytes(b"sha256:" + b"1" * 64 + b"\n")
            (case / "selection.json").write_bytes(b'{"fixture_source_sha256":"PRIVATE_CANARY"}\n')
            intent = case / "allocation-intents" / token
            intent.write_bytes((case / "selected").read_bytes() + (case / "selection.json").read_bytes())

            def refuse():
                result = subprocess.run(
                    [
                        "bash",
                        "-eu",
                        "-c",
                        'source "$1"; if provider_service_format_request "$2" "$3"; then exit 0; else printf "%s\\n" "$provider_service_format_diagnostic" >&2; exit 2; fi',
                        "formats",
                        "/workspace/tools/provider_service_qualification.sh",
                        str(root),
                        token,
                    ],
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 2)
                self.assertIn(b"Cannot admit format allocation", result.stderr)
                self.assertNotIn(b"PRIVATE_CANARY", result.stdout + result.stderr)

            refuse()
            (case / "admitted").write_bytes(b"admitted\n")
            (case / "selected").write_bytes(b"sha256:" + b"2" * 64 + b"\n")
            refuse()
            intent.unlink()
            intent.symlink_to(case / "selection.json")
            refuse()
            for active in (b"../json\n", b"json\nold-case\n", b"PRIVATE_CANARY" * 4):
                (root / "active-case").write_bytes(active)
                refuse()
