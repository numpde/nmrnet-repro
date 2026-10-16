"""Scientific fixture/producer provenance checks and retained source crosswalk.

Source: secs-repro 34f5915c1534278bbf61b4c6592afa44cf94ab08,
tests/input/normative/test_fixture_provenance.py. Corpus file equality, bytes,
archive parent and exact generator identity apply to NMRNet extraction. The
NMRium member/reference-vector assertions require absent capabilities and are
not replaced with an empty corpus. Extraction receipts bind exact producer bytes.
SECS per-file textual attribution is separately verified in the independently
authored artifacts/source-attribution.json. Its source artifact
identities, archive parents and qualification/preparation consumer pointers reuse
the retained reference authority; they do not claim local tools trained models.
https://github.com/numpde/secs-repro/blob/34f5915c1534278bbf61b4c6592afa44cf94ab08/tests/input/normative/test_fixture_provenance.py
"""

from tools.container_guard import require_container

require_container()

import copy
import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from repository_checks.quality.sources import ROOT
from repository_checks.science import artifact
from tools import artifacts


class ScientificArtifactRepositoryTests(unittest.TestCase):
    def test_public_release_and_all_archive_selections_are_closed_and_owned(self):
        artifact.verify_release(ROOT)

    def test_retained_reference_is_bound_to_public_artifacts_and_original_producer_bytes(self):
        artifact.verify_reference(ROOT)

    def test_retained_source_artifacts_have_complete_independently_authored_attribution(self):
        artifact.verify_attribution(ROOT)


class FixtureProvenanceDetectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        archive = self.root / "fixture.zip"
        self.archive_path = archive
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("fixture/data.txt", b"reviewed data\n")
        raw = archive.read_bytes()
        self.archive = {
            "name": "fixture.zip",
            "url": "https://zenodo.org/api/records/1/files/fixture.zip/content",
            "size": len(raw),
            "md5": hashlib.md5(raw, usedforsecurity=False).hexdigest(),
        }
        self.selection = {"members": ["fixture/data.txt"], "max_bytes": 32}
        self.output = self.root / "extracted"
        artifacts.extract(archive, self.archive, self.selection, self.output)
        self.receipt_path = self.output / "provenance.json"
        self.receipt = artifact.json_document(self.receipt_path)

    def with_producer_fixture(self):
        """Add expected producer facts to the small mutation fixture only."""
        receipt = copy.deepcopy(self.receipt)
        receipt["producer_sources"] = {"tools/artifacts.py": artifact.file_identity(ROOT / "tools/artifacts.py")}
        self.receipt_path.write_text(json.dumps(receipt))
        return receipt

    def test_actual_extractor_records_exact_producer_bytes(self):
        producers = artifact.verify_extraction(self.output, self.archive, self.selection, archive_path=self.archive_path)
        artifact.verify_producers(ROOT, producers)

    def test_every_corpus_file_has_one_record_and_extra_missing_or_changed_files_fail(self):
        self.with_producer_fixture()
        artifact.verify_extraction(self.output, self.archive, self.selection, archive_path=self.archive_path)
        extra = self.output / "fixture/extra.txt"
        extra.write_text("unreviewed")
        with self.assertRaisesRegex(ValueError, "exactly one"):
            artifact.verify_extraction(self.output, self.archive, self.selection, archive_path=self.archive_path)
        extra.unlink()
        member = self.output / "fixture/data.txt"
        member.write_text("altered data\n")
        with self.assertRaisesRegex(ValueError, "bytes differ"):
            artifact.verify_extraction(self.output, self.archive, self.selection, archive_path=self.archive_path)
        member.unlink()
        with self.assertRaisesRegex(ValueError, "exactly one"):
            artifact.verify_extraction(self.output, self.archive, self.selection, archive_path=self.archive_path)

    def test_symlink_and_duplicate_receipt_fields_are_rejected(self):
        self.with_producer_fixture()
        member = self.output / "fixture/data.txt"
        member.unlink()
        member.symlink_to(self.receipt_path)
        with self.assertRaisesRegex(ValueError, "symlink"):
            artifact.verify_extraction(self.output, self.archive, self.selection, archive_path=self.archive_path)
        self.receipt_path.write_text('{"members":{},"members":{}}')
        with self.assertRaisesRegex(ValueError, "duplicate fields"):
            artifact.json_document(self.receipt_path)

    def test_archive_parent_and_selection_identity_drift_fail(self):
        self.with_producer_fixture()
        wrong = {**self.archive, "md5": "0" * 32}
        with self.assertRaisesRegex(ValueError, "archive identity"):
            artifact.verify_extraction(self.output, wrong, self.selection, archive_path=self.archive_path)
        for selection in (
            {"members": [], "max_bytes": 32},
            {"members": self.selection["members"] * 2, "max_bytes": 32},
            {"members": ["fixture/other.txt"], "max_bytes": 32},
            {**self.selection, "max_bytes": 1},
        ):
            with self.subTest(selection=selection), self.assertRaises(ValueError):
                artifact.verify_extraction(self.output, self.archive, selection, archive_path=self.archive_path)

    def test_valid_but_wrong_archive_sha256_and_wrong_zip_bytes_are_rejected(self):
        receipt = self.with_producer_fixture()
        # silent-defaults: allow-contract -- Replacing the first digest digit with a different valid hex digit preserves its grammar while testing rejection against actual archive bytes; proof=repository_checks/test_fixture_provenance.py::FixtureProvenanceDetectionTests.test_valid_but_wrong_archive_sha256_and_wrong_zip_bytes_are_rejected
        wrong_digest = ("0" if receipt["archive_sha256"][0] != "0" else "1") + receipt["archive_sha256"][1:]
        self.receipt_path.write_text(json.dumps({**receipt, "archive_sha256": wrong_digest}))
        with self.assertRaisesRegex(ValueError, "actual ZIP bytes"):
            artifact.verify_extraction(self.output, self.archive, self.selection, archive_path=self.archive_path)
        self.receipt_path.write_text(json.dumps(receipt))
        self.archive_path.write_bytes(self.archive_path.read_bytes() + b"changed archive bytes")
        with self.assertRaisesRegex(ValueError, "archive bytes differ"):
            artifact.verify_extraction(self.output, self.archive, self.selection, archive_path=self.archive_path)

    def test_historical_execution_and_preparation_producer_inventories_are_exact(self):
        reference = artifact.json_document(ROOT / "artifacts/reference-cpu-float32.json")
        for phase in ("provenance", "preparation_execution"):
            execution = reference[phase]
            # The independent byte verifier is covered by actual source/Git
            # checks. Isolate the inventory boundary here so missing Git cannot
            # turn a missing-producer mutation into a false rejection proof.
            with patch.object(artifact, "verify_producers") as verify:
                artifact.verify_reference_execution(ROOT, execution, phase)
                verify.assert_called_once_with(ROOT, execution["tool_sources"], execution["repository_revision"])
            missing = copy.deepcopy(execution)
            missing["tool_sources"].pop(next(iter(missing["tool_sources"])))
            extra = copy.deepcopy(execution)
            extra["tool_sources"]["tools/unknown.py"] = next(iter(execution["tool_sources"].values()))
            for mutated in (missing, extra):
                with (
                    self.subTest(phase=phase, sources=sorted(mutated["tool_sources"])),
                    patch.object(artifact, "verify_producers") as verify,
                ):
                    with self.assertRaisesRegex(ValueError, "complete exact historical producer input set"):
                        artifact.verify_reference_execution(ROOT, mutated, phase)
                    verify.assert_not_called()

    def test_generator_identity_changes_and_unknown_or_missing_producers_fail(self):
        receipt = self.with_producer_fixture()
        producer_root = self.root / "producer"
        (producer_root / "tools").mkdir(parents=True)
        producer = producer_root / "tools/artifacts.py"
        producer.write_bytes((ROOT / "tools/artifacts.py").read_bytes())
        artifact.verify_producers(producer_root, receipt["producer_sources"])
        producer.write_bytes(producer.read_bytes() + b"\nchanged\n")
        with self.assertRaisesRegex(ValueError, "Producer bytes differ"):
            artifact.verify_producers(producer_root, receipt["producer_sources"])
        for producers in ({}, {**receipt["producer_sources"], "tools/unknown.py": artifact.file_identity(producer)}):
            self.receipt_path.write_text(json.dumps({**receipt, "producer_sources": producers}))
            with self.assertRaisesRegex(ValueError, "producer input set"):
                artifact.verify_extraction(self.output, self.archive, self.selection, archive_path=self.archive_path)

    def test_release_loader_rejects_unknown_archive_and_root_fields(self):
        declaration = artifact.json_document(ROOT / "artifacts/release.json")
        variants = [dict(declaration, unknown_authority="unreviewed")]
        changed = copy.deepcopy(declaration)
        changed["files"][0]["unknown_authority"] = "unreviewed"
        variants.append(changed)
        path = self.root / "release.json"
        for document in variants:
            with self.subTest(document=document):
                path.write_text(json.dumps(document))
                with self.assertRaises(ValueError):
                    artifacts.load_declaration(path)

    def attribution_fixture(self):
        """Use explicit fixture text, borrowing only existing reference identities.

        These strings are not attribution facts about the public scientific
        files. No attribution declaration is written into the repository.
        Artifact roles are enumerated independently of the checker's inventory.
        """
        reference_path = ROOT / "artifacts/reference-cpu-float32.json"
        reference = artifact.json_document(reference_path)
        names = ["source_artifacts/data", "source_artifacts/dictionary"]
        names.extend(
            f"elements/{element}/artifacts/{role}"
            for element in ("H", "C")
            for role in ("checkpoint", "scaler", "infer_log", "finetune_log")
        )
        records = []
        for name in names:
            facts = reference
            for part in name.split("/"):
                facts = facts[part]
            if name.startswith("source_artifacts/"):
                archive = facts["archive"]["name"]
                parent_digest = facts["archive_sha256"]
                consumer = "preparation_execution"
            else:
                archive_facts = reference["elements"][name.split("/")[1]]["checkpoint_archive"]
                archive = archive_facts["name"]
                parent_digest = archive_facts["sha256"]
                consumer = "provenance"
            records.append(
                {
                    "artifact_ref": name,
                    "identity": {"size": facts["size"], "sha256": facts["sha256"]},
                    "description": "Fixture description only",
                    "origin": {
                        "author": None,
                        "generator": "Fixture generator only",
                        "generator_evidence": "Fixture evidence reference only",
                        "licence": "Fixture licence identifier only",
                        "licence_text": "Fixture licence text only",
                        "basis": "Fixture basis only",
                        "parents": [{"archive": archive, "sha256": parent_digest}],
                        "consumer_execution": consumer,
                    },
                }
            )
        digest = hashlib.sha256(reference_path.read_bytes()).hexdigest()
        return {"reference_sha256": digest, "files": records}, reference, digest

    def test_attribution_inventory_is_complete_unique_and_binds_existing_bytes_and_parents(self):
        document, reference, digest = self.attribution_fixture()
        artifact.verify_attribution_document(document, reference, digest)
        variants = []
        missing = copy.deepcopy(document)
        missing["files"].pop()
        variants.append(missing)
        duplicate = copy.deepcopy(document)
        duplicate["files"].append(copy.deepcopy(duplicate["files"][0]))
        variants.append(duplicate)
        for field, value in (("artifact_ref", "source_artifacts/unknown"), ("identity", {"size": 1, "sha256": "0" * 64})):
            changed = copy.deepcopy(document)
            changed["files"][0][field] = value
            variants.append(changed)
        for field, value in (
            ("parents", [{"archive": "unknown.zip", "sha256": "0" * 64}]),
            ("parents", [{"archive": document["files"][0]["origin"]["parents"][0]["archive"], "sha256": "0" * 64}]),
            ("consumer_execution", "provenance"),
        ):
            changed = copy.deepcopy(document)
            changed["files"][0]["origin"][field] = value
            variants.append(changed)
        variants.append({**document, "reference_sha256": "0" * 64})
        for changed in variants:
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                artifact.verify_attribution_document(changed, reference, digest)

    def test_attribution_requires_description_author_and_every_original_origin_text_field(self):
        document, reference, digest = self.attribution_fixture()
        for field in ("generator", "generator_evidence", "licence", "licence_text", "basis"):
            for value in ("", " ", None, 0):
                changed = copy.deepcopy(document)
                changed["files"][0]["origin"][field] = value
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    artifact.verify_attribution_document(changed, reference, digest)
        for value in ("", " ", None, 0):
            changed = copy.deepcopy(document)
            changed["files"][0]["description"] = value
            with self.subTest(description=value), self.assertRaises(ValueError):
                artifact.verify_attribution_document(changed, reference, digest)
        for value in ("", " ", 0, {}):
            changed = copy.deepcopy(document)
            changed["files"][0]["origin"]["author"] = value
            with self.subTest(author=value), self.assertRaises(ValueError):
                artifact.verify_attribution_document(changed, reference, digest)
        # Original SECS origin admits both explicitly unknown and named authors.
        for author in (None, "Fixture author only"):
            changed = copy.deepcopy(document)
            changed["files"][0]["origin"]["author"] = author
            artifact.verify_attribution_document(changed, reference, digest)

    def test_attribution_borrows_reference_consumer_verification_without_a_parallel_producer_oracle(self):
        document, reference, _ = self.attribution_fixture()
        with (
            patch.object(artifact, "json_document", side_effect=[document, reference]),
            patch.object(artifact, "regular_bytes", return_value=(ROOT / "artifacts/reference-cpu-float32.json").read_bytes()),
            patch.object(artifact, "verify_reference") as verify,
        ):
            artifact.verify_attribution(ROOT)
            verify.assert_called_once_with(ROOT)
        changed = copy.deepcopy(document)
        changed["files"][0]["origin"]["consumer_execution"] = "unknown"
        with (
            patch.object(artifact, "json_document", side_effect=[changed, reference]),
            patch.object(artifact, "regular_bytes", return_value=(ROOT / "artifacts/reference-cpu-float32.json").read_bytes()),
            patch.object(artifact, "verify_reference") as verify,
        ):
            with self.assertRaisesRegex(ValueError, "consumer execution"):
                artifact.verify_attribution(ROOT)
            verify.assert_not_called()
