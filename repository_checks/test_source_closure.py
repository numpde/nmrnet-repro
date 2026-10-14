"""Preserve NMRPeak source-closure and image-context rejection scenarios.

Crosswalk: nmrpeak-repro f8055ab1065795fd46f8c1539e894cf9773f5495,
tests/repository/test_nmrpeak_source.py (all nine methods),
test_nmrpeak_runtime_sources.py (source revision methods), and
test_nmrpeak_provider_image.py (complete context ownership).
Fixtures use actual local Git commits, never a mocked committed-source oracle.
https://github.com/numpde/nmrpeak-repro/blob/f8055ab1065795fd46f8c1539e894cf9773f5495/tests/repository/test_nmrpeak_source.py
"""

from tools.container_guard import require_container

require_container()

import copy
import hashlib
import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from repository_checks.quality.sources import ROOT
from repository_checks.science import artifact, source


@contextmanager
def committed_fixture():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        upstream = root / "upstream"
        (upstream / "runtime").mkdir(parents=True)
        (upstream / "LICENSE").write_text("Reviewed licence\n")
        worker = upstream / "runtime/worker.py"
        worker.write_text("VALUE = 0\n")
        source.git(upstream, "init", "--quiet")
        source.git(upstream, "add", ".")
        source.git(
            upstream,
            "-c",
            "user.name=Check",
            "-c",
            "user.email=check@example.test",
            "commit",
            "--quiet",
            "-m",
            "Prior scientific source",
        )
        worker.write_text("VALUE = 1\n")
        source.git(upstream, "add", ".")
        source.git(
            upstream,
            "-c",
            "user.name=Check",
            "-c",
            "user.email=check@example.test",
            "commit",
            "--quiet",
            "-m",
            "Reviewed scientific source",
        )
        revision = source.git(upstream, "rev-parse", "HEAD").decode().strip()
        declaration = root / "source.paths"
        declaration.write_text(f"source_revision {revision}\nLICENSE\nruntime\n")
        manifest = root / "source.sha256"
        manifest.write_text(
            "".join(
                f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(upstream)}\n"
                for path in (upstream / "LICENSE", worker)
            )
        )
        yield upstream, declaration, manifest


class ScientificSourceRepositoryTests(unittest.TestCase):
    def test_declared_committed_and_live_source_are_identical(self):
        source.verify_parent_gitlink(ROOT)

    def test_committed_context_contains_exactly_the_declared_inference_inputs(self):
        source.verify_image_inputs(ROOT)

    def test_source_closure_identity_matches_the_qualified_reference(self):
        artifact.verify_reference_source_qualification(ROOT)


class ReferenceSourceQualificationDetectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "artifacts").mkdir()
        for name in (
            "nmrnet-source.paths",
            "nmrnet-source.sha256",
            "reference-cpu-float32.json",
            "reference-source-qualification.json",
        ):
            (self.root / "artifacts" / name).write_bytes((ROOT / "artifacts" / name).read_bytes())
        self.path = self.root / "artifacts/reference-source-qualification.json"
        self.document = artifact.json_document(self.path)

    def test_missing_reference_declaration_manifest_and_supplement_are_rejected(self):
        for name in (
            "reference-cpu-float32.json",
            "nmrnet-source.paths",
            "nmrnet-source.sha256",
            "reference-source-qualification.json",
        ):
            path = self.root / "artifacts" / name
            raw = path.read_bytes()
            path.unlink()
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "Required provenance input"):
                artifact.verify_reference_source_qualification(self.root)
            path.write_bytes(raw)

    def test_reference_image_revision_inventory_and_capture_binding_mutations_fail_before_git(self):
        variants = []
        for field in ("reference_sha256", "source_paths_sha256", "source_closure_sha256"):
            changed = copy.deepcopy(self.document)
            changed[field] = "0" * 64
            variants.append(changed)
        for field, value in (("id", "sha256:" + "0" * 64), ("upstream_revision", "0" * 40), ("files", {})):
            changed = copy.deepcopy(self.document)
            changed["image_observation"][field] = value
            variants.append(changed)
        for mutation in ("missing", "extra", "changed"):
            changed = copy.deepcopy(self.document)
            files = changed["image_observation"]["files"]
            if mutation == "missing":
                files.pop("LICENSE")
            elif mutation == "extra":
                files["uninmr/unreviewed.py"] = "0" * 64
            else:
                files["LICENSE"] = "0" * 64
            variants.append(changed)
        changed = copy.deepcopy(self.document)
        changed["evidence"]["files"].pop("qualify.command")
        variants.append(changed)
        changed = copy.deepcopy(self.document)
        changed["evidence"]["files"]["qualify.exit"] = {"size": 2, "sha256": hashlib.sha256(b"1\n").hexdigest()}
        variants.append(changed)
        variants.extend(
            ({**self.document, "qualification": "historical_execution"}, {**self.document, "unreviewed_authority": True})
        )
        for document in variants:
            self.path.write_text(json.dumps(document))
            with self.subTest(document=document), patch.object(source, "verify_parent_gitlink") as verify:
                with self.assertRaises(ValueError):
                    artifact.verify_reference_source_qualification(self.root)
                verify.assert_not_called()
        self.path.write_text('{"qualification":"one","qualification":"two"}')
        with self.assertRaisesRegex(ValueError, "duplicate fields"):
            artifact.verify_reference_source_qualification(self.root)

    def test_historical_gitlinks_are_checked_against_real_commits(self):
        revision, _ = source.read_declaration(self.root / source.DECLARATION)
        source.git(self.root, "init", "--quiet")
        source.git(self.root, "update-index", "--add", "--cacheinfo", f"160000,{revision},NMRNetplusplus")
        source.git(
            self.root,
            "-c",
            "user.name=Check",
            "-c",
            "user.email=check@example.test",
            "commit",
            "--quiet",
            "-m",
            "Fixture historical source",
        )
        reviewed = source.git(self.root, "rev-parse", "HEAD").decode().strip()
        source.git(self.root, "update-index", "--cacheinfo", f"160000,{'a' * 40},NMRNetplusplus")
        source.git(
            self.root,
            "-c",
            "user.name=Check",
            "-c",
            "user.email=check@example.test",
            "commit",
            "--quiet",
            "-m",
            "Fixture wrong historical source",
        )
        wrong = source.git(self.root, "rev-parse", "HEAD").decode().strip()
        reference_path = self.root / "artifacts/reference-cpu-float32.json"
        reference = artifact.json_document(reference_path)
        for phase in ("provenance", "preparation_execution"):
            reference[phase]["repository_revision"] = reviewed
        for rejected_phase in (None, "provenance", "preparation_execution"):
            changed = copy.deepcopy(reference)
            if rejected_phase is not None:
                changed[rejected_phase]["repository_revision"] = wrong
            reference_path.write_text(json.dumps(changed))
            qualification = {**self.document, "reference_sha256": hashlib.sha256(reference_path.read_bytes()).hexdigest()}
            self.path.write_text(json.dumps(qualification))
            with patch.object(source, "verify_parent_gitlink"), patch.object(artifact, "verify_reference") as verify:
                if rejected_phase is None:
                    artifact.verify_reference_source_qualification(self.root)
                    verify.assert_called_once_with(self.root)
                else:
                    with self.assertRaisesRegex(ValueError, f"historical {rejected_phase} gitlink"):
                        artifact.verify_reference_source_qualification(self.root)
                    verify.assert_not_called()


class SourceClosureDetectionTests(unittest.TestCase):
    def test_declared_committed_and_live_fixture_are_identical(self):
        with committed_fixture() as arguments:
            source.verify_source(*arguments)

    def test_modified_live_source_is_rejected(self):
        with committed_fixture() as (upstream, declaration, manifest):
            worker = upstream / "runtime/worker.py"
            worker.write_bytes(worker.read_bytes() + b"\n")
            with self.assertRaisesRegex(ValueError, "Live source content drift"):
                source.verify_source(upstream, declaration, manifest)

    def test_untracked_file_inside_closure_is_rejected(self):
        with committed_fixture() as (upstream, declaration, manifest):
            (upstream / "runtime/unreviewed.py").write_text("pass\n")
            with self.assertRaisesRegex(ValueError, "Live source inventory"):
                source.verify_source(upstream, declaration, manifest)

    def test_materialized_build_source_rejects_an_extra_file(self):
        with committed_fixture() as (upstream, declaration, manifest):
            (upstream / "runtime/unreviewed.txt").write_text("unreviewed\n")
            with self.assertRaisesRegex(ValueError, "Live source inventory"):
                source.verify_materialized(upstream, declaration, manifest)

    def test_symlinked_source_file_is_rejected(self):
        with committed_fixture() as (upstream, declaration, manifest):
            worker = upstream / "runtime/worker.py"
            worker.unlink()
            worker.symlink_to("../LICENSE")
            with self.assertRaisesRegex(ValueError, "symlink"):
                source.verify_source(upstream, declaration, manifest)

    def test_absolute_declaration_root_is_rejected(self):
        with committed_fixture() as (upstream, declaration, manifest):
            declaration.write_text(declaration.read_text() + "/tmp\n")
            with self.assertRaisesRegex(ValueError, "invalid root"):
                source.verify_source(upstream, declaration, manifest)

    def test_special_source_file_is_rejected(self):
        with committed_fixture() as (upstream, declaration, manifest):
            worker = upstream / "runtime/worker.py"
            worker.unlink()
            os.mkfifo(worker)
            with self.assertRaisesRegex(ValueError, "special file"):
                source.verify_source(upstream, declaration, manifest)

    def test_wrong_submodule_revision_is_rejected(self):
        with committed_fixture() as (upstream, declaration, manifest):
            source.git(upstream, "checkout", "--quiet", "HEAD^")
            with self.assertRaisesRegex(ValueError, "declared source revision"):
                source.verify_source(upstream, declaration, manifest)

    def test_manifest_content_drift_is_rejected(self):
        with committed_fixture() as (upstream, declaration, manifest):
            digest, rest = manifest.read_text().split("  ", 1)
            changed = ("0" if digest[0] != "0" else "1") + digest[1:]
            manifest.write_text(changed + "  " + rest)
            with self.assertRaisesRegex(ValueError, "Committed source content drift"):
                source.verify_source(upstream, declaration, manifest)

    def test_missing_source_and_manifest_inventory_drift_are_rejected(self):
        with committed_fixture() as (upstream, declaration, manifest):
            original = manifest.read_text()
            manifest.write_text(original.splitlines()[0] + "\n")
            with self.assertRaisesRegex(ValueError, "committed closure"):
                source.verify_source(upstream, declaration, manifest)
            manifest.write_text(original)
            (upstream / "LICENSE").unlink()
            with self.assertRaisesRegex(ValueError, "missing"):
                source.verify_source(upstream, declaration, manifest)

    def test_empty_duplicate_overlapping_and_unsafe_roots_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            declaration = Path(temporary) / "source.paths"
            for roots in ("", "runtime\nruntime\n", "runtime\nruntime/nested\n", "../runtime\n", "./runtime\n"):
                with self.subTest(roots=roots):
                    declaration.write_text("source_revision " + "a" * 40 + "\n" + roots)
                    with self.assertRaises(ValueError):
                        source.read_declaration(declaration)

    def test_image_input_inventory_cannot_be_empty_duplicated_unsafe_or_omit_policy(self):
        with tempfile.TemporaryDirectory() as temporary:
            declaration = Path(temporary) / "image-inputs.txt"
            for text in ("", "tools/a.py\ntools/a.py\n", "../escape\n", "tools/a.py\n", "NMRNetplusplus/uninmr/infer.py\n"):
                with self.subTest(text=text):
                    declaration.write_text(text)
                    with self.assertRaises(ValueError):
                        source.read_image_inputs(declaration)

    def test_context_has_exact_committed_inventory_bytes_modes_and_identity(self):
        with committed_fixture() as (upstream, declaration, manifest):
            revision, roots = source.read_declaration(declaration)
            with tempfile.TemporaryDirectory() as temporary:
                context = Path(temporary)
                identity = source.materialize_committed(upstream, revision, roots, context)
                self.assertEqual(set(source.read_manifest(manifest)), set(source.live_paths(context, roots)))
                self.assertRegex(identity, r"^sha256:[0-9a-f]{64}$")
                for name, (mode, object_id) in source.committed_entries(upstream, revision, roots).items():
                    self.assertEqual(source.git(upstream, "cat-file", "blob", object_id), (context / name).read_bytes())
                    self.assertEqual(0o755 if mode == "100755" else 0o644, (context / name).stat().st_mode & 0o777)
                worker = context / "runtime/worker.py"
                worker.chmod(0o755)
                self.assertNotEqual(source.context_digest(context), identity)
                worker.chmod(0o644)
                worker.write_bytes(worker.read_bytes() + b"\n")
                self.assertNotEqual(source.context_digest(context), identity)

    def test_symlinked_declaration_ancestor_is_rejected(self):
        with committed_fixture() as (upstream, _declaration, _manifest):
            nested = upstream / "runtime/nested"
            nested.mkdir()
            (nested / "worker.py").write_text("pass\n")
            (upstream / "alias").symlink_to("runtime", target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlink"):
                source.live_paths(upstream, ("alias/nested",))
