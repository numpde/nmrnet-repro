"""Actual contained Git proves the sanitized projection and rejected grants."""

from tools.container_guard import require_container

require_container()

import os
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


class GitInputTests(unittest.TestCase):
    def setUp(self):
        self.git_binary = shutil.which("git")
        self.assertIsNotNone(self.git_binary, msg="Git projection proofs require actual Git in the contained test image")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repository = self.root / "repository"
        self.repository.mkdir()
        self.environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        self.environment.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_NO_REPLACE_OBJECTS": "1"})
        self.git(self.repository, "init", "--quiet")
        tools = self.repository / "tools"
        tools.mkdir()
        (tools / "producer.py").write_text("VALUE = 'original producer'\n")
        self.git(self.repository, "add", "tools")
        self.commit(self.repository)
        self.prior = self.git(self.repository, "rev-parse", "HEAD").strip()
        self.upstream = self.repository / "NMRNetplusplus"
        self.upstream.mkdir()
        self.git(self.upstream, "init", "--quiet")
        (self.upstream / "LICENSE").write_text("reviewed licence\n")
        (self.upstream / "uninmr").mkdir()
        (self.upstream / "uninmr/worker.py").write_text("VALUE = 1\n")
        self.git(self.upstream, "add", ".")
        self.commit(self.upstream)
        (self.repository / ".gitmodules").write_text(
            '[submodule "nmrnet"]\npath = NMRNetplusplus\nurl = https://example.invalid/upstream\n'
        )
        self.git(self.repository, "add", ".gitmodules", "NMRNetplusplus")
        self.commit(self.repository)
        self.git(self.repository, "submodule", "absorbgitdirs")
        self.git(self.repository, "config", "remote.origin.url", "https://private-config-marker.invalid/repository")
        self.destination = self.root / "projection"
        self.script = Path("tools/check_git_inputs.sh").resolve()

    def git(self, repository, *arguments):
        result = subprocess.run(
            [self.git_binary, "-C", str(repository), *arguments],
            env=self.environment,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, msg=result.stderr)
        return result.stdout

    def commit(self, repository):
        self.git(
            repository,
            "-c",
            "user.name=Check",
            "-c",
            "user.email=check@example.test",
            "commit",
            "--quiet",
            "-m",
            "Reviewed fixture",
        )

    def prepare(self, **overrides):
        environment = {key: value for key, value in self.environment.items() if not key.startswith("GIT_")}
        return subprocess.run(
            ["bash", str(self.script), str(self.repository), str(self.destination)],
            env=environment | overrides,
            text=True,
            capture_output=True,
            check=False,
        )

    def assert_rejected(self, **overrides):
        result = self.prepare(**overrides)
        self.assertEqual(2, result.returncode, msg=result.stderr)
        self.assertEqual("", result.stdout)
        self.assertFalse(self.destination.exists())
        return result

    def test_projection_resolves_current_gitlink_upstream_and_historical_producer(self):
        result = self.prepare()
        self.assertEqual(0, result.returncode, msg=result.stderr)
        self.assertEqual("", result.stdout)
        self.assertEqual({path.name for path in self.destination.iterdir()}, {"HEAD", "config", "objects", "refs", "modules"})
        self.assertEqual(self.destination.stat().st_mode & 0o777, 0o700)
        self.assertNotIn("private-config-marker", (self.destination / "config").read_text())
        self.assertEqual([], list((self.destination / "refs").iterdir()))
        workspace = self.root / "workspace"
        workspace.mkdir()
        shutil.copytree(self.destination, workspace / ".git")
        projected_upstream = workspace / "NMRNetplusplus"
        projected_upstream.mkdir()
        (projected_upstream / ".git").write_bytes((self.upstream / ".git").read_bytes())
        # Real launcher mounts these two stores read-only; copying their objects
        # here proves the metadata's Git topology without a nested engine grant.
        for original, projected in (
            (self.repository / ".git/objects", workspace / ".git/objects"),
            (self.repository / ".git/modules/nmrnet/objects", workspace / ".git/modules/nmrnet/objects"),
        ):
            shutil.copytree(original, projected, dirs_exist_ok=True)
        parent_head = self.git(self.repository, "rev-parse", "HEAD").strip()
        upstream_head = self.git(self.upstream, "rev-parse", "HEAD").strip()
        self.assertEqual(self.git(workspace, "rev-parse", "HEAD").strip(), parent_head)
        self.assertEqual(self.git(projected_upstream, "rev-parse", "HEAD").strip(), upstream_head)
        self.assertEqual(
            self.git(workspace, "ls-tree", "HEAD", "--", "NMRNetplusplus").strip(),
            f"160000 commit {upstream_head}\tNMRNetplusplus",
        )
        self.assertEqual(
            "VALUE = 'original producer'\n", self.git(workspace, "cat-file", "blob", f"{self.prior}:tools/producer.py")
        )
        self.assertEqual("VALUE = 1\n", self.git(projected_upstream, "cat-file", "blob", "HEAD:uninmr/worker.py"))

    def test_wrong_gitlink_rejects_before_metadata_creation(self):
        (self.upstream / "uninmr/worker.py").write_text("VALUE = 2\n")
        self.git(self.upstream, "add", ".")
        self.commit(self.upstream)
        self.assert_rejected()

    def test_changed_or_linked_upstream_pointer_is_not_followed(self):
        pointer = self.upstream / ".git"
        original = pointer.read_bytes()
        pointer.write_text("gitdir: /unreviewed/private\n")
        self.assert_rejected()
        target = self.root / "pointer"
        target.write_bytes(original)
        pointer.unlink()
        pointer.symlink_to(target)
        self.assert_rejected()

    def test_git_metadata_and_object_links_reject_before_creation(self):
        for relative in (".git/HEAD", ".git/config", ".git/objects", ".git/modules/nmrnet/objects"):
            with self.subTest(relative=relative):
                path = self.repository / relative
                saved = self.root / "saved"
                path.rename(saved)
                path.symlink_to(saved, target_is_directory=saved.is_dir())
                try:
                    self.assert_rejected()
                finally:
                    path.unlink()
                    saved.rename(path)
        (self.repository / ".git/objects/unreviewed").symlink_to(self.root / "outside")
        self.assert_rejected()

    def test_alternates_and_shared_directory_redirects_are_rejected(self):
        for relative in (".git/objects/info/alternates", ".git/modules/nmrnet/objects/info/alternates", ".git/commondir"):
            with self.subTest(relative=relative):
                path = self.repository / relative
                path.write_text("/unreviewed/private\n")
                try:
                    self.assert_rejected()
                finally:
                    path.unlink()

    def test_ambient_git_redirect_and_replacement_variables_are_rejected(self):
        for variable in ("GIT_DIR", "GIT_WORK_TREE", "GIT_REPLACE_REF_BASE", "GIT_NO_REPLACE_OBJECTS", "GIT_CONFIG_COUNT"):
            with self.subTest(variable=variable):
                result = self.assert_rejected(**{variable: "private-override-marker"})
                self.assertNotIn("private-override-marker", result.stderr)

    def test_unrelated_git_author_identity_does_not_change_source_admission(self):
        result = self.prepare(GIT_AUTHOR_NAME="unused author identity", GIT_COMMITTER_EMAIL="unused@example.test")
        self.assertEqual(0, result.returncode, msg=result.stderr)
        self.assertEqual("", result.stdout)

    def test_existing_destination_or_link_is_preserved(self):
        self.destination.mkdir()
        sentinel = self.destination / "retained"
        sentinel.write_text("existing metadata\n")
        result = self.prepare()
        self.assertEqual(2, result.returncode)
        self.assertEqual("existing metadata\n", sentinel.read_text())
        sentinel.unlink()
        self.destination.rmdir()
        target = self.root / "target"
        target.mkdir()
        self.destination.symlink_to(target, target_is_directory=True)
        result = self.prepare()
        self.assertEqual(2, result.returncode)
        self.assertTrue(self.destination.is_symlink())
        self.assertEqual([], list(target.iterdir()))

    def test_missing_commit_or_object_store_rejects_before_creation(self):
        head = self.repository / ".git/HEAD"
        original = head.read_bytes()
        head.write_text("0" * 40 + "\n")
        self.assert_rejected()
        head.write_bytes(original)
        objects = self.repository / ".git/modules/nmrnet/objects"
        objects.rename(self.root / "missing-objects")
        self.assert_rejected()

    def test_missing_promisor_object_cannot_execute_a_remote_helper(self):
        """The positive control proves actual lazy dispatch, without network.

        /bin/sh interprets the owned temporary script because checker scratch is
        noexec. The helper writes only a fixture marker and intentionally fails.
        """
        tree = self.git(self.repository, "rev-parse", "HEAD^{tree}").strip()
        marker = self.root / "remote-called"
        helper = self.root / "remote-helper.sh"
        helper.write_text("#!/bin/sh\nprintf called > " + shlex.quote(str(marker)) + "\nexit 1\n")
        for key, value in (
            ("core.repositoryformatversion", "1"),
            ("extensions.partialClone", "origin"),
            ("remote.origin.promisor", "true"),
            ("remote.origin.partialCloneFilter", "blob:none"),
            ("remote.origin.url", "ext::/bin/sh " + str(helper) + " private-remote-credential-marker"),
            ("protocol.ext.allow", "always"),
        ):
            self.git(self.repository, "config", key, value)
        object_path = self.repository / ".git/objects" / tree[:2] / tree[2:]
        self.assertTrue(object_path.is_file(), msg="fixture tree must be a loose actual Git object")
        object_path.unlink()
        result = self.assert_rejected()
        self.assertIn("committed upstream gitlink could not be read", result.stderr)
        self.assertNotIn("private-remote-credential-marker", result.stderr)
        self.assertFalse(marker.exists(), msg="read-only source admission dispatched its promisor remote")
        control = subprocess.run(
            [self.git_binary, "-C", str(self.repository), "ls-tree", "HEAD"],
            env=self.environment | {"GIT_ALLOW_PROTOCOL": "ext", "GIT_TERMINAL_PROMPT": "0"},
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(control.returncode, 0)
        self.assertEqual("called", marker.read_text(), msg="control must reach the actual lazy-fetch helper")
