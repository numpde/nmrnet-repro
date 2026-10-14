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

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import tempfile
import unittest

from repository_checks.science import source


ROOT = Path(__file__).resolve().parents[1]


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
        source.git(upstream, "-c", "user.name=Check", "-c", "user.email=check@example.test",
                   "commit", "--quiet", "-m", "Prior scientific source")
        worker.write_text("VALUE = 1\n")
        source.git(upstream, "add", ".")
        source.git(upstream, "-c", "user.name=Check", "-c", "user.email=check@example.test",
                   "commit", "--quiet", "-m", "Reviewed scientific source")
        revision = source.git(upstream, "rev-parse", "HEAD").decode().strip()
        declaration = root / "source.paths"
        declaration.write_text(f"source_revision {revision}\nLICENSE\nruntime\n")
        manifest = root / "source.sha256"
        manifest.write_text("".join(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(upstream)}\n"
                                    for path in (upstream / "LICENSE", worker)))
        yield upstream, declaration, manifest


class ScientificSourceRepositoryTests(unittest.TestCase):
    def test_declared_committed_and_live_source_are_identical(self):
        source.verify_parent_gitlink(ROOT)

    def test_committed_context_contains_exactly_the_declared_inference_inputs(self):
        source.verify_image_inputs(ROOT)

    def test_source_closure_identity_matches_the_qualified_reference(self):
        import json
        reference = json.loads(source.regular_bytes(ROOT / "artifacts/reference-cpu-float32.json"))
        identity = "sha256:" + hashlib.sha256(source.regular_bytes(ROOT / source.MANIFEST)).hexdigest()
        self.assertEqual(reference["provenance"]["source_closure_sha256"], identity)


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
            for text in ("", "tools/a.py\ntools/a.py\n", "../escape\n", "tools/a.py\n",
                         "NMRNetplusplus/uninmr/infer.py\n"):
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
                self.assertEqual(set(source.live_paths(context, roots)), set(source.read_manifest(manifest)))
                self.assertRegex(identity, r"^sha256:[0-9a-f]{64}$")
                for name, (mode, object_id) in source.committed_entries(upstream, revision, roots).items():
                    self.assertEqual((context / name).read_bytes(), source.git(upstream, "cat-file", "blob", object_id))
                    self.assertEqual((context / name).stat().st_mode & 0o777, 0o755 if mode == "100755" else 0o644)
                worker = context / "runtime/worker.py"
                worker.chmod(0o755)
                self.assertNotEqual(source.context_digest(context), identity)
                worker.chmod(0o644)
                worker.write_bytes(worker.read_bytes() + b"\n")
                self.assertNotEqual(source.context_digest(context), identity)

    def test_symlinked_declaration_ancestor_is_rejected(self):
        with committed_fixture() as (upstream, declaration, manifest):
            nested = upstream / "runtime/nested"
            nested.mkdir()
            (nested / "worker.py").write_text("pass\n")
            (upstream / "alias").symlink_to("runtime", target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlink"):
                source.live_paths(upstream, ("alias/nested",))
