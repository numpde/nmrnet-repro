"""Exercise committed geometry-image capture and stale-image rejection offline."""

from tools.container_guard import require_container

require_container()

import hashlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from repository_checks.fixtures.containers.fixture import LauncherFixture
from repository_checks.quality.sources import ROOT


class GeometryImageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.fixture = LauncherFixture(self.root)
        self.addCleanup(self.fixture.dispose)
        # Use real Git to prove captured blobs, rather than a second model of Git.
        (self.root / "bin/git").unlink()
        (self.root / "bin/git").symlink_to(shutil.which("git"))
        shutil.rmtree(self.root / ".git")
        self.git("init", "-q", "--initial-branch=main")
        self.inputs = (
            "containers/geometry/Dockerfile",
            "containers/geometry/Dockerfile.dockerignore",
            "requirements/preparation.lock",
            "tools/artifacts.py",
            "tools/container_guard.py",
            "tools/rdkit_environment.py",
            "tools/wheelhouse.py",
        )
        for name in (*self.inputs, "tools/geometry_image.sh"):
            destination = self.root / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, destination)
        cache = tempfile.TemporaryDirectory()
        self.addCleanup(cache.cleanup)
        self.wheelhouse = Path(cache.name)
        self.fixture.environment["NMRNET_WHEELHOUSE_INPUT"] = str(self.wheelhouse)
        shutil.rmtree(self.root / ".work/wheelhouse")
        (self.wheelhouse / "preparation").mkdir()
        (self.wheelhouse / "preparation/fixture.whl").write_bytes(b"frozen fixture wheel")
        (self.root / "tests/integration/rdkit_runtime").mkdir(parents=True)
        self.git("add", "--", *self.inputs)
        self.commit()

    def git(self, *arguments):
        return subprocess.run(
            ["git", "-C", str(self.root), *arguments],
            capture_output=True,
            check=True,
            env=self.fixture.environment,
        ).stdout

    def commit(self):
        self.git(
            "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture", "--", *self.inputs
        )

    def test_build_captures_committed_bytes_and_publishes_only_admitted_image(self):
        self.assertFalse((self.root / ".work/preparation").exists())
        result = self.fixture.run("container.sh", "geometry/image/build")
        self.assertEqual(0, result.returncode, msg=result.stderr)
        record = self.root / ".work/preparation/image-id"
        self.assertEqual("sha256:" + "b" * 64 + "\n", record.read_text())
        evidence = self.fixture.build_evidence()
        self.assertEqual(
            hashlib.sha256((ROOT / "requirements/preparation.lock").read_bytes()).hexdigest(),
            evidence["manifest"]["requirements/preparation.lock"]["sha256"],
        )
        self.assertEqual({"fixture.whl"}, set(evidence["wheelhouse_manifest"]))
        self.assertFalse(Path(evidence["context"]).parent.exists())
        for call in self.fixture.calls():
            self.assertEqual([], call["client_config"]["entries"])
            self.assertFalse(Path(call["client_config"]["path"]).parent.exists())

    def test_dirty_build_input_stops_before_engine_dispatch(self):
        with (self.root / "requirements/preparation.lock").open("ab") as stream:
            stream.write(b"# uncommitted change\n")
        result = self.fixture.run("container.sh", "geometry/image/build")
        self.assertEqual(2, result.returncode)
        self.assertEqual([], self.fixture.calls())
        self.assertFalse((self.root / ".work/preparation/image-id").exists())

    def test_build_failure_preserves_previous_record_and_failure_evidence(self):
        record = self.root / ".work/preparation/image-id"
        record.parent.mkdir()
        previous = "sha256:" + "c" * 64 + "\n"
        record.write_text(previous)
        result = self.fixture.run("container.sh", "geometry/image/build", additions={"NMRNET_FAKE_BUILD_STATUS": "37"})
        self.assertEqual(37, result.returncode)
        self.assertEqual(previous, record.read_text())
        self.assertIn("RDKit image build", result.stderr)

    def test_changed_committed_inputs_reject_previous_image_before_worker_launch(self):
        self.assertEqual(0, self.fixture.run("container.sh", "geometry/image/build").returncode)
        build = self.fixture.calls()[0]["arguments"]
        old_identity = build[build.index("--build-arg") + 1].removeprefix("GEOMETRY_INPUTS=")
        with (self.root / "requirements/preparation.lock").open("ab") as stream:
            stream.write(b"# changed provenance with the same package versions\n")
        self.commit()
        result = self.fixture.run(
            "container.sh", "test/integration/rdkit-worker", additions={"NMRNET_FAKE_GEOMETRY_INPUTS": old_identity}
        )
        self.assertEqual(2, result.returncode, msg=result.stderr)
        self.assertIn("build-input label", result.stderr)
        self.assertFalse(any(call["arguments"][:1] == ["run"] for call in self.fixture.calls()))

    def test_wrong_built_image_label_does_not_publish_a_record(self):
        result = self.fixture.run(
            "container.sh", "geometry/image/build", additions={"NMRNET_FAKE_GEOMETRY_INPUTS": "sha256:" + "d" * 64}
        )
        self.assertEqual(2, result.returncode)
        self.assertIn("build-input label", result.stderr)
        self.assertFalse((self.root / ".work/preparation/image-id").exists())
