"""Image retirement protects actual reference owners before any Engine deletion."""

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

ROOT = Path("/workspace")


def image(letter):
    return "sha256:" + letter * 64


class ImageRetirementTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        tools = self.root / "tools"
        tools.mkdir()
        for name in ("image_retirement.sh", "image_lifecycle.sh"):
            shutil.copyfile(ROOT / "tools" / name, tools / name)
        (tools / "docker_client.sh").write_text("""docker_client_prepare() {
    engine=(python3 "$repository_root/tools/engine.py" "$repository_root")
}
docker_client_cleanup() { :; }
""")
        (tools / "inference_native.sh").write_text("""inference_native() {
    shift 3
    "$@"
}
""")
        (tools / "engine.py").write_text("""import json,sys
from pathlib import Path
root=Path(sys.argv[1]); args=sys.argv[2:]
state=json.loads((root/"state.json").read_text())
with (root/"calls.jsonl").open("a") as stream: stream.write(json.dumps(args)+"\\n")
if state.get("fail")==args[0]: raise SystemExit(7)
if args[0]=="compose":
    assert args[-3:]==["config","--no-interpolate","--images"]
    recipe=Path(args[args.index("--file")+1])
    for service in json.loads(recipe.read_text())["services"].values(): print(service["image"])
elif args[0]=="ps":
    if "--filter" not in args or args[-1].endswith(state["used"]): print("1"*64)
elif args[0]=="inspect": print(state["used"])
elif args[:2]==["image","ls"]:
    role=args[-1].removeprefix("label=io.nmrnet-repro.").removesuffix("-inputs")
    for identifier in state["images"][role]: print(identifier)
elif args[:2]==["image","inspect"]:
    aliases = "1 0" if args[-1]==state["tagged"] else "0 0"
    calls = [json.loads(line) for line in (root/"calls.jsonl").read_text().splitlines()]
    inspections = sum(call[:2] == ["image", "inspect"] and call[-1] == args[-1] for call in calls)
    if "Created" in args[args.index("--format") + 1]:
        print(aliases, state.get("created", {}).get(args[-1], "2001-01-01T00:00:00Z"))
    else:
        print("1 0" if inspections > 1 and args[-1] == state.get("tagged_on_recheck") else aliases)
elif args[:2]==["image","rm"]:
    assert args[2]=="--no-prune" and len(args)==4
    with (root/"removed").open("a") as stream: stream.write(args[-1]+"\\n")
else: raise SystemExit(91)
""")
        for role, letter in (("provider", "a"), ("preparation", "b"), ("inference", "c"), ("checker", "9")):
            path = self.root / ".work" / role
            path.mkdir(parents=True)
            (path / "image-id").write_text(image(letter) + "\n")
        self.deployment = self.root / "secrets/deployments/stopped"
        bundle = self.deployment / "bundles/generation-selected"
        bundle.mkdir(parents=True)
        (bundle / "compose.json").write_text(json.dumps({"services": {"provider": {"image": image("d")}}}))
        (self.deployment / "current").symlink_to("bundles/generation-selected")
        self.state = {
            "images": {
                "provider": [image(x) for x in "adef0"],
                "geometry": [image("b")],
                "inference": [image("c")],
                "checker": [image("9")],
            },
            "used": image("e"),
            "tagged": image("0"),
        }
        self.save_state()

    def save_state(self):
        (self.root / "state.json").write_text(json.dumps(self.state))

    def retire(self):
        return subprocess.run(
            ["bash", str(self.root / "tools/image_retirement.sh")], capture_output=True, text=True, timeout=5, check=False
        )

    def removed(self):
        path = self.root / "removed"
        return path.read_text().splitlines() if path.exists() else []

    def test_only_obsolete_owned_images_are_removed_with_no_parent_pruning(self):
        result = self.retire()
        self.assertEqual(0, result.returncode, msg=result.stderr)
        self.assertEqual(self.removed(), [image("f")])
        calls = [json.loads(line) for line in (self.root / "calls.jsonl").read_text().splitlines()]
        self.assertEqual([call for call in calls if call[:2] == ["image", "rm"]], [["image", "rm", "--no-prune", image("f")]])

    def test_failed_inventory_and_broken_selected_recipe_remove_nothing(self):
        for fault in ("inspect", "compose", "image", "broken_selection"):
            with self.subTest(fault=fault):
                if "fail" in self.state:
                    del self.state["fail"]
                self.save_state()
                if fault == "broken_selection":
                    (self.deployment / "current").unlink()
                    (self.deployment / "current").symlink_to("bundles/generation-missing")
                else:
                    self.state["fail"] = fault
                    self.save_state()
                result = self.retire()
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual([], self.removed())

    def test_retained_unselected_generation_protects_its_images(self):
        retained = self.deployment / "bundles/generation-retained"
        retained.mkdir()
        (retained / "compose.json").write_text(json.dumps({"services": {"provider": {"image": image("f")}}}))
        result = self.retire()
        self.assertEqual(0, result.returncode, msg=result.stderr)
        self.assertEqual([], self.removed())

    def test_checker_record_is_protected_and_older_unreferenced_checker_is_retired(self):
        self.state["images"]["checker"].append(image("8"))
        self.save_state()
        result = self.retire()
        self.assertEqual(0, result.returncode, msg=result.stderr)
        self.assertCountEqual(self.removed(), [image("f"), image("8")])

    def test_incomplete_retained_generation_blocks_every_removal(self):
        (self.deployment / "bundles/generation-incomplete").mkdir()
        result = self.retire()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("provider/deployment/retire-generations", result.stderr)
        self.assertEqual([], self.removed())

    def test_recent_unselected_image_is_preserved(self):
        self.state["created"] = {image("f"): datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}
        self.save_state()
        result = self.retire()
        self.assertEqual(0, result.returncode, msg=result.stderr)
        self.assertEqual([], self.removed())

    def test_engine_creation_time_accepts_explicit_utc_offset(self):
        self.state["created"] = {image("f"): "2001-01-01T03:00:00.123456789+03:00"}
        self.save_state()
        result = self.retire()
        self.assertEqual(0, result.returncode, msg=result.stderr)
        self.assertEqual(self.removed(), [image("f")])

    def test_fractional_creation_time_is_not_rounded_down_at_day_boundary(self):
        binary = self.root / "bin"
        binary.mkdir()
        clock = binary / "date"
        clock.symlink_to(ROOT / "tests/fake_image_retirement_date.py")
        self.state["created"] = {image("f"): "2001-01-01T00:00:00.1Z"}
        self.save_state()
        with mock.patch.dict(os.environ, {"PATH": str(binary) + os.pathsep + os.environ["PATH"]}):
            result = self.retire()
        self.assertEqual(0, result.returncode, msg=result.stderr)
        self.assertEqual([], self.removed())

    def test_unknown_or_future_creation_time_never_removes_image(self):
        for created in ("not-a-time", "2001-01-01T00:00:00+24:00", "2001-01-01T00:00Z", "2999-01-01T00:00:00Z"):
            with self.subTest(created=created):
                self.state["created"] = {image("f"): created}
                self.save_state()
                result = self.retire()
                self.assertEqual(result.returncode == 0, created.startswith("2999"))
                self.assertEqual([], self.removed())

    def test_alias_added_after_inventory_is_preserved(self):
        self.state["tagged_on_recheck"] = image("f")
        self.save_state()
        result = self.retire()
        self.assertEqual(0, result.returncode, msg=result.stderr)
        self.assertEqual([], self.removed())

    def test_checkout_lease_protects_the_build_to_record_publication_gap(self):
        publisher = subprocess.Popen(
            [
                "bash",
                "-eu",
                "-c",
                """repository_root=$1
source "$repository_root/tools/image_lifecycle.sh"
image_lifecycle_lock shared
printf '%s\\n' "$2" > "$repository_root/new-image"
touch "$repository_root/ready"
until [[ -f $repository_root/release ]]; do sleep 0.01; done
mv "$repository_root/new-image" "$repository_root/.work/provider/image-id"
""",
                "publication",
                str(self.root),
                image("f"),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 2
            while not (self.root / "ready").exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue((self.root / "ready").exists())
            result = self.retire()
            self.assertEqual(75, result.returncode, msg=result.stderr)
            self.assertFalse((self.root / "calls.jsonl").exists())
            (self.root / "release").touch()
            _, stderr = publisher.communicate(timeout=2)
            self.assertEqual(0, publisher.returncode, msg=stderr)
            result = self.retire()
            self.assertEqual(0, result.returncode, msg=result.stderr)
            self.assertEqual(self.removed(), [image("a")])
        finally:
            if publisher.poll() is None:
                publisher.kill()
            publisher.communicate()

    def test_unreadable_retention_owners_are_not_treated_as_absent(self):
        for directory in (self.deployment, self.root / ".work/provider"):
            with self.subTest(directory=directory.name):
                directory.chmod(0)
                try:
                    result = self.retire()
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual([], self.removed())
                finally:
                    directory.chmod(0o700)
