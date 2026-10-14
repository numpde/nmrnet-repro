"""Complete authored image source discovery and independent reviewed inventory.

API Compose build-service and checker-image assembly assertions are specific to
that source repository. NMRNet has direct Dockerfile consumers and a launcher
that mounts source trees; its corresponding completeness assertion reads the
launcher's printed mount grant without contacting the container engine.
"""

from tools.container_guard import require_container

require_container()

# Port source: https://github.com/numpde/nmr-api-v1/blob/dbd81a81ee92bd04d3223f4389a26891181cc0cd/repository_checks/test_source_inventory.py
# All four SourceInventoryUnitTests copied unchanged. Container exact inventory,
# independent authority and deterministic diagnostic serialization retained.
# Compose source membership/interpolation/build-service coverage is inapplicable:
# there are no Compose sources or service build declarations in this repository.
import ast
import json
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import unittest

from .source_inventory import (SourceInventoryError, discover_source_files,
                               inventory_delta, require_exact_inventory)
from .sources import ROOT

REVIEWED_DOCKERFILES = frozenset({"containers/inference/Dockerfile",
                                "containers/packages/Dockerfile.lock"})
REVIEWED_CONTAINER_SUPPORT_FILES = frozenset({
    "containers/inference/Dockerfile.dockerignore",
    "containers/packages/Dockerfile.lock.dockerignore",
})
REVIEWED_CONTAINER_FILES = REVIEWED_DOCKERFILES | REVIEWED_CONTAINER_SUPPORT_FILES
DOCKERFILE_SYNTAX = ("# syntax=docker/dockerfile:1.7@sha256:"
                     "a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e")
ROOT_ENTRY_MANIFEST = Path("/repository-source-inventory/root-entries")
# The coordinator explicitly keeps checkout control, credentials and writable
# acquisition/runtime state outside source-check authority. Capturing their
# names in the manifest proves classification without exposing their contents.
PRIVATE_ROOT_ENTRIES = frozenset({".git", ".agents", ".codex", ".aws", ".work"})


def root_entry_visibility_findings(manifest, root):
    """Reject omitted public entries and root links hidden by engine binds.

    The contained runner captures type/name metadata using host find, with
    %y-tab-%f-NUL records. This retains the source suite's complete tree proof
    while granting only public source contents to the checker.
    """
    records = manifest.split("\0")
    if not records or records[-1] != "":
        raise ValueError("root-entry manifest must end with NUL")
    expected = {}
    findings = []
    for record in records[:-1]:
        kind, separator, name = record.partition("\t")
        if not separator or not name or "/" in name or name in {".", ".."}:
            raise ValueError("root-entry manifest has an invalid root name")
        if name in expected:
            raise ValueError("root-entry manifest repeats a root name")
        expected[name] = kind
        if name in PRIVATE_ROOT_ENTRIES:
            continue
        if kind not in {"f", "d"}:
            findings.append(f"{name}: public source root is not a regular file or directory")
            continue
        path = root / name
        if not path.exists():
            findings.append(f"{name}: public source root is invisible to repository checks")
        elif path.is_symlink() or (kind == "f" and not path.is_file()) or (kind == "d" and not path.is_dir()):
            findings.append(f"{name}: visible source root type disagrees with host inventory")
    for path in sorted(root.iterdir()):
        if path.name not in expected:
            findings.append(f"{path.name}: visible root entry is absent from host inventory")
    return findings

class SourceInventoryUnitTests(unittest.TestCase):
    def test_root_visibility_reports_new_source_caches_and_hidden_links(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "tools").mkdir()
            (root / "stale.txt").write_text("fixture")
            manifest = "d\ttools\0f\tnew-source.py\0d\t.pytest_cache\0l\tlinked.py\0d\t.work\0d\t.aws\0"
            self.assertEqual([
                "new-source.py: public source root is invisible to repository checks",
                ".pytest_cache: public source root is invisible to repository checks",
                "linked.py: public source root is not a regular file or directory",
                "stale.txt: visible root entry is absent from host inventory",
            ], root_entry_visibility_findings(manifest, root))

    def test_root_visibility_rejects_malformed_duplicate_and_mismatched_records(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for manifest in ("f\tname", "f\t../escape\0", "f\tname\0f\tname\0", "f\t\0"):
                with self.subTest(manifest=manifest), self.assertRaises(ValueError):
                    root_entry_visibility_findings(manifest, root)
            (root / "file").write_text("fixture")
            self.assertEqual(["file: visible source root type disagrees with host inventory"],
                             root_entry_visibility_findings("d\tfile\0", root))

    def test_discovery_is_recursive_and_inventory_delta_is_directional(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "compose" / "nested").mkdir(parents=True)
            (root / "compose" / "base.yml").write_text("services: {}\n")
            (root / "compose" / "nested" / "debug.yaml").write_text(
                "services: {}\n"
            )

            discovered = discover_source_files(root=root, subtree="compose")

        self.assertEqual(
            discovered,
            (
                "compose/base.yml",
                "compose/nested/debug.yaml",
            ),
        )
        self.assertEqual(
            (
                ("compose/missing.yml",),
                ("compose/nested/debug.yaml",),
            ),
            inventory_delta(
                discovered=discovered,
                reviewed=("compose/base.yml", "compose/missing.yml"),
            ),
        )

    def test_exact_inventory_names_missing_and_unclassified_files(self) -> None:
        with self.assertRaisesRegex(
            SourceInventoryError,
            re.escape(
                "compose source inventory is not exact; "
                "missing=['compose/missing.yml']; "
                "unclassified=['compose/new.yml']"
            ),
        ):
            require_exact_inventory(
                label="compose source",
                discovered=("compose/base.yml", "compose/new.yml"),
                reviewed=("compose/base.yml", "compose/missing.yml"),
            )

    def test_discovery_rejects_file_and_directory_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            compose = root / "compose"
            compose.mkdir()
            target = root / "target.yml"
            target.write_text("services: {}\n")
            (compose / "linked.yml").symlink_to(target)

            with self.assertRaisesRegex(
                SourceInventoryError,
                "compose/linked.yml must not be a symlink",
            ):
                discover_source_files(root=root, subtree="compose")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            compose = root / "compose"
            compose.mkdir()
            target = root / "target"
            target.mkdir()
            (compose / "linked").symlink_to(target, target_is_directory=True)

            with self.assertRaisesRegex(
                SourceInventoryError,
                "compose/linked must not be a symlink",
            ):
                discover_source_files(root=root, subtree="compose")

    def test_discovery_rejects_invalid_or_missing_source_roots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for subtree in ("", "/compose", "compose/", "compose/../other"):
                with self.subTest(subtree=subtree):
                    with self.assertRaises((TypeError, ValueError)):
                        discover_source_files(root=root, subtree=subtree)
            with self.assertRaisesRegex(
                SourceInventoryError,
                "compose source root must be a directory",
            ):
                discover_source_files(root=root, subtree="compose")


class SourceInventoryRepositoryTests(unittest.TestCase):
    def test_host_root_inventory_is_complete_and_visible_to_source_checks(self):
        self.assertTrue(ROOT_ENTRY_MANIFEST.is_file(),
                        msg="repository checks require a read-only host root-entry inventory")
        findings = root_entry_visibility_findings(ROOT_ENTRY_MANIFEST.read_bytes().decode("utf-8"), ROOT)
        self.assertEqual([], findings, msg="\n".join(findings))
    def test_container_source_inventory_is_exact_and_fully_classified(self):
        discovered = discover_source_files(root=ROOT, subtree="containers")
        require_exact_inventory(label="container source", discovered=discovered,
                                reviewed=REVIEWED_CONTAINER_FILES)

    def test_container_sources_keep_original_generic_hardening(self):
        # Source CMD/ENTRYPOINT bans protect Compose-owned API commands. Here
        # image-owned default commands are the supported boundary; those two
        # domain assertions have no local counterpart. Other source assertions
        # remain literal, including frontend and direct FROM digest policy.
        for dockerfile in sorted(REVIEWED_DOCKERFILES):
            text = (ROOT / dockerfile).read_text(encoding="utf-8")
            with self.subTest(dockerfile=dockerfile):
                self.assertTrue(text.startswith(DOCKERFILE_SYNTAX))
                # Same instruction recognition as the source suite's
                # test_container_posture.dockerfile_instructions helper.
                instructions = []
                for line in text.splitlines():
                    if not line.strip() or line.lstrip().startswith("#"):
                        continue
                    match = re.match(r"([A-Z][A-Z0-9_-]*)\b", line)
                    if match is not None:
                        instructions.append(match.group(1))
                self.assertIn("FROM", instructions)
                self.assertIn("USER", instructions)
                self.assertNotRegex(text, r"(?m)^ADD\s+")
                self.assertNotRegex(text, r"(?m)^COPY\s+.*(?:^|\s)\.(?:\s|$)")
                self.assertNotRegex(text, r"(?m)^COPY\s+.*\*")
                self.assertNotRegex(text, r"--mount=type=(?:cache|secret|ssh)\b")
                external_images = []
                named_stages = set()
                for line in text.splitlines():
                    if not line.startswith("FROM "):
                        continue
                    fields = shlex.split(line)
                    images = [field for field in fields[1:] if not field.startswith("--")]
                    self.assertTrue(images)
                    image = images[0]
                    if image not in named_stages:
                        external_images.append(image)
                    if len(fields) >= 4 and fields[-2].upper() == "AS":
                        named_stages.add(fields[-1])
                self.assertTrue(external_images)
                for image in external_images:
                    self.assertRegex(image, r"@sha256:[0-9a-f]{64}$")
                users = re.findall(r"(?m)^USER\s+(\S+)\s*$", text)
                self.assertTrue(users)
                self.assertNotIn(users[-1], {"root", "0", "0:0"})

    def test_authored_python_roots_are_complete_safe_trees(self):
        for subtree in ("tools", "tests", "repository_checks"):
            with self.subTest(subtree=subtree):
                self.assertTrue(discover_source_files(root=ROOT, subtree=subtree))

    def test_repository_checker_grants_complete_read_only_source_trees(self):
        result = subprocess.run(["bash", str(ROOT / "tools/container.sh"),
                                 "--print", "test/repository"],
                                text=True, capture_output=True, check=True)
        arguments = shlex.split(result.stdout)
        mounts = [arguments[index + 1] for index, value in enumerate(arguments)
                  if value == "--mount"]
        for subtree in ("tools", "tests", "repository_checks", "containers"):
            self.assertIn(f"type=bind,src={ROOT / subtree},dst=/workspace/{subtree},readonly", mounts)
        for mount in mounts:
            self.assertNotIn("docker.sock", mount)
            self.assertTrue(mount.endswith(",readonly"), msg=mount)

    def test_inventory_authorities_are_independent_of_discovery(self):
        tree = ast.parse((ROOT / "repository_checks/quality/test_source_inventory.py").read_text())
        assignments = {target.id: statement.value for statement in tree.body
                       if isinstance(statement, ast.Assign)
                       for target in statement.targets if isinstance(target, ast.Name)}
        for authority in ("REVIEWED_DOCKERFILES", "REVIEWED_CONTAINER_FILES"):
            value = assignments[authority]
            called_names = {node.func.id for node in ast.walk(value)
                            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
            self.assertNotIn("discover_source_files", called_names)
        self.assertEqual(REVIEWED_CONTAINER_FILES,
                         REVIEWED_DOCKERFILES | REVIEWED_CONTAINER_SUPPORT_FILES)

    def test_expected_source_inventory_is_json_serializable(self):
        rendered = json.dumps({"containers": sorted(REVIEWED_CONTAINER_FILES)},
                              separators=(",", ":"), sort_keys=True)
        self.assertEqual(json.loads(rendered)["containers"], sorted(REVIEWED_CONTAINER_FILES))
