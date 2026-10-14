"""Strict lock port with mutation evidence for each NMRPeak lock obligation.

Source: nmrpeak-repro f8055ab1065795fd46f8c1539e894cf9773f5495,
repository_checks/nmrpeak_lock.py. No missing tooling or red lock is exempted.
https://github.com/numpde/nmrpeak-repro/blob/f8055ab1065795fd46f8c1539e894cf9773f5495/repository_checks/nmrpeak_lock.py
"""

from tools.container_guard import require_container

require_container()

import json
import tempfile
import unittest
from pathlib import Path

from repository_checks.quality.sources import ROOT
from repository_checks.science import dependency
from repository_checks.science.source import regular_bytes
from tools import wheelhouse

DIGEST = "a" * 64


def lock_text(body):
    return ("\n".join(dependency.HEADER) + "\n" + body).encode()


class DependencyOwnershipTests(unittest.TestCase):
    def test_lock_matches_all_exact_direct_intent_and_artifact_hashes(self):
        dependency.check_lock(ROOT, ROOT / "requirements/inference.lock")

    def test_advisory_evidence_owns_exact_applied_lock_inputs_and_package_inventory(self):
        dependency.check_advisory_binding(ROOT)

    def test_unicore_source_declaration_is_one_closed_exact_version_revision_and_archive_identity(self):
        path = ROOT / "requirements/unicore-source.json"
        document = json.loads(regular_bytes(path))
        self.assertEqual({"name", "version", "commit", "url", "size", "sha256"}, set(document))
        self.assertIsInstance(document["version"], str)
        self.assertRegex(document["version"], r"^[0-9]+(?:\.[0-9]+)+$")
        self.assertEqual(document, wheelhouse.source_identity(path))


class DependencyDetectionTests(unittest.TestCase):
    BODY = "example==1.0 \\\n    --hash=sha256:" + DIGEST + "\n"

    def test_valid_exact_canonical_lock_is_admitted(self):
        self.assertEqual({"example": ("==1.0", (DIGEST,))}, dependency.locked_requirements(lock_text(self.BODY)))

    def test_missing_empty_header_noncanonical_text_and_unhashed_entries_fail(self):
        for raw in (b"", b"example==1.0\n", lock_text(self.BODY).replace(b"\n", b"\r\n"),
                    lock_text(self.BODY).rstrip(), lock_text("example==1.0\n"),
                    lock_text("example==1.0 \\\n"), lock_text("")):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                dependency.locked_requirements(raw)

    def test_duplicate_names_unsorted_packages_and_hashes_bad_continuations_fail(self):
        cases = (self.BODY * 2, self.BODY + self.BODY.replace("example", "Example"),
                 self.BODY.replace("example", "zeta") + self.BODY,
                 "example==1.0 \\\n    --hash=sha256:" + DIGEST + " \\\n    --hash=sha256:" + DIGEST + "\n",
                 "example==1.0 \\\n    --hash=sha256:" + "b" * 64 + " \\\n    --hash=sha256:" + DIGEST + "\n",
                 self.BODY.rstrip() + " \\\n", self.BODY.replace(DIGEST, "sha1:bad"),
                 self.BODY.replace("==1.0", ">=1.0"))
        for body in cases:
            with self.subTest(body=body), self.assertRaises(ValueError):
                dependency.locked_requirements(lock_text(body))

    def test_direct_intent_rejects_empty_ranges_duplicates_options_and_unhashed_urls(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "intent.in"
            for text in ("", "example>=1.0\n", "example==1.0\nExample==1.0\n", "--extra-index-url https://other.test\n",
                         "example @ https://example.test/example.whl\n"):
                with self.subTest(text=text):
                    path.write_text(text)
                    with self.assertRaises(ValueError):
                        dependency.direct_requirements([path])

    def test_source_reader_rejects_unknown_declaration_authority(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "unicore-source.json"
            document = json.loads(regular_bytes(ROOT / "requirements/unicore-source.json"))
            document["unreviewed_source"] = "https://other.test/archive.tar.gz"
            path.write_text(json.dumps(document))
            with self.assertRaises(ValueError):
                wheelhouse.source_identity(path)

    def test_direct_intent_version_source_hash_and_transitive_url_drift_fail(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "requirements").mkdir()
            intent = root / "requirements/inference.in"
            intent.write_text("example==1.0\n")
            (root / "requirements/build.in").write_text("# no additional direct packages\n")
            lock = root / "requirements/inference.lock"
            lock.write_bytes(lock_text(self.BODY))
            dependency.check_lock(root, lock)
            for body in (self.BODY.replace("==1.0", "==2.0"), self.BODY.replace("example", "other"),
                         self.BODY + "other @ https://unreviewed.test/other.whl \\\n    --hash=sha256:" + DIGEST + "\n"):
                with self.subTest(body=body):
                    lock.write_bytes(lock_text(body))
                    with self.assertRaises(ValueError):
                        dependency.check_lock(root, lock)
            intent.write_text("example @ https://reviewed.test/example.whl#sha256=" + DIGEST + "\n")
            for url, digest in (("https://wrong.test/example.whl", DIGEST), ("https://reviewed.test/example.whl", "b" * 64)):
                lock.write_bytes(lock_text(f"example @ {url} \\\n    --hash=sha256:{digest}\n"))
                with self.assertRaises(ValueError):
                    dependency.check_lock(root, lock)
