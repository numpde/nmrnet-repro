"""The preparation launcher grants offline reads and one dedicated output mount."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


def decode_command(text):
    # Bash %q uses ANSI quoting for the multiline admission program.
    decoded = subprocess.run(["bash", "-c", text.splitlines()[-1]],
                             env={**os.environ, "BASH_FUNC_env%%": '() { printf "%s\\0" "$@"; }'},
                             capture_output=True, check=True)
    return decoded.stdout.decode().rstrip("\0").split("\0")


def assert_image_admission(test, args, module):
    program = args[args.index("-c") + 1]
    for drift in (None, "inference.lock", "unicore-source.json"):
        with test.subTest(drift=drift):
            paths = {}
            for name in ("inference.lock", "unicore-source.json"):
                for root in ("/opt/inference", "/input"):
                    path = test.root / (root.rsplit("/", 1)[1] + "-" + name)
                    path.write_text("drift" if drift == name and root == "/input" else name)
                    paths[root + "/" + name] = path
            with patch("pathlib.Path", side_effect=lambda name: paths[name]), \
                    patch("runpy.run_module") as run, patch.object(sys, "argv", ["-c", module]):
                if drift:
                    with test.assertRaisesRegex(ValueError, drift.replace(".", r"\.")):
                        exec(compile(program, "baseline-image-admission", "exec"), {})
                    run.assert_not_called()
                else:
                    exec(compile(program, "baseline-image-admission", "exec"), {})
                    run.assert_called_once_with(module, run_name="__main__")


class BaselineLauncherTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in ("tools", "artifacts", "requirements", ".work/inference", ".work/prepared/data", ".work/prepared/weight"):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        (self.root / "tools/baseline.sh").write_bytes(Path("tools/baseline.sh").read_bytes())
        for name in ("tools/artifacts.py", "tools/baseline_prepare.py", "artifacts/release.json",
                     "artifacts/data.selection.json", "artifacts/weight.selection.json",
                     ".work/prepared/data/provenance.json", ".work/prepared/weight/provenance.json",
                     "requirements/inference.lock", "requirements/unicore-source.json"):
            (self.root / name).write_text("{}")
        (self.root / ".work/inference/image-id").write_text("sha256:" + "a" * 64 + "\n")
        self.repository_revision = "d" * 40
        self.git_function = '() { if [[ "$*" == *rev-parse* ]]; then echo "' + self.repository_revision + '"; else echo "160000 ' + "b" * 40 + ' 0 NMRNetplusplus"; fi; }'

    def command(self, action="prepare"):
        return subprocess.run(["bash", str(self.root / "tools/baseline.sh"), "--print", action],
                              env={**os.environ, "DOCKER_HOST": "tcp://ambient.invalid:2375",
                                   "DOCKER_CONTEXT": "ambient", "ARCHIVE": "ignored.zip",
                                   "BASH_FUNC_git%%": self.git_function},
                              text=True, capture_output=True)

    def test_prepare_is_offline_bounded_and_uses_recorded_image(self):
        result = self.command()
        self.assertEqual(result.returncode, 0, result.stderr)
        args = decode_command(result.stdout)
        self.assertEqual(args[args.index("--network") + 1], "none")
        self.assertEqual(args[args.index("--pull") + 1], "never")
        self.assertEqual(args[args.index("--host") + 1], "unix:///var/run/docker.sock")
        self.assertIn("--read-only", args)
        self.assertIn("no-new-privileges:true", args)
        self.assertIn("sha256:" + "a" * 64, args)
        self.assertEqual(args[args.index("--user") + 1], f"{os.getuid()}:{os.getgid()}")
        self.assertEqual(args[args.index("--cap-drop") + 1], "ALL")
        for flag in ("--cpus", "--memory", "--memory-swap", "--pids-limit", "--tmpfs"):
            self.assertIn(flag, args)
        self.assertNotIn("--gpus", args)
        self.assertNotIn("--device", args)
        self.assertNotIn("--privileged", args)
        self.assertFalse((self.root / ".work/baseline").exists())
        self.assertEqual(args[args.index("--image-id") + 1], "sha256:" + "a" * 64)
        self.assertEqual(args[args.index("--repository-revision") + 1], self.repository_revision)
        sources = [args[index + 1] for index, item in enumerate(args) if item == "--tool-source"]
        self.assertEqual(set(sources), {"tools/artifacts.py", "tools/baseline_prepare.py"})

    def test_only_declared_inputs_and_dedicated_output_are_mounted(self):
        args = decode_command(self.command().stdout)
        mounts = [args[index + 1] for index, item in enumerate(args) if item == "--mount"]
        expected = {
            "tools/artifacts.py": "/workspace/tools/artifacts.py",
            "tools/baseline_prepare.py": "/workspace/tools/baseline_prepare.py",
            "artifacts/release.json": "/input/release.json",
            "artifacts/data.selection.json": "/input/data.selection.json",
            "artifacts/weight.selection.json": "/input/weight.selection.json",
            "requirements/inference.lock": "/input/inference.lock",
            "requirements/unicore-source.json": "/input/unicore-source.json",
            ".work/prepared/data": "/prepared/data",
            ".work/prepared/weight": "/prepared/weight",
        }
        self.assertEqual(set(mounts), {
            f"type=bind,src={self.root / source},dst={destination},readonly"
            for source, destination in expected.items()
        } | {f"type=bind,src={self.root / '.work/baseline'},dst=/baseline"})
        self.assertEqual(args[args.index("--workdir") + 1], "/workspace")
        self.assertIn("tools.baseline_prepare", args)
        for flag, value in (("--prepared-root", "/prepared"), ("--declaration", "/input/release.json"),
                            ("--data-selection", "/input/data.selection.json"),
                            ("--dictionary-selection", "/input/weight.selection.json"),
                            ("--output", "/baseline/prepared"), ("--count", "2"),
                            ("--max-atoms", "128"), ("--max-tokens", "130")):
            self.assertEqual(args[args.index(flag) + 1], value)

    def test_prepare_checks_both_embedded_inputs_before_scientific_imports(self):
        assert_image_admission(self, decode_command(self.command().stdout), "tools.baseline_prepare")

    def test_missing_and_mutable_image_identity_reject_before_engine_access(self):
        image = self.root / ".work/inference/image-id"
        image.unlink()
        self.assertNotEqual(self.command().returncode, 0)
        image.write_text("mutable:tag\n")
        self.assertNotEqual(self.command().returncode, 0)

    def test_symlinked_input_or_output_parent_rejects(self):
        for name in ("artifacts/data.selection.json", ".work/inference/image-id",
                     ".work/prepared/data/provenance.json", ".work/prepared/data", ".work/baseline"):
            with self.subTest(name=name):
                path = self.root / name
                saved = path.with_name(path.name + ".saved")
                if path.exists():
                    path.rename(saved)
                path.symlink_to(saved)
                result = self.command()
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                path.unlink()
                if saved.exists():
                    saved.rename(path)

    def test_missing_prepared_receipt_rejects(self):
        (self.root / ".work/prepared/data/provenance.json").unlink()
        result = self.command()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("provenance.json", result.stderr)

    def test_existing_output_is_preserved_and_rejected(self):
        output = self.root / ".work/baseline/prepared"
        output.mkdir(parents=True)
        (output / "keep").write_text("existing")
        result = self.command()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((output / "keep").read_text(), "existing")
        self.assertEqual(result.stdout, "")

    def test_unknown_lane_is_rejected(self):
        self.assertNotEqual(self.command("unknown").returncode, 0)


class BaselineRunLauncherTests(unittest.TestCase):
    def setUp(self):
        BaselineLauncherTests.setUp(self)
        for name in (".work/baseline/prepared", ".work/prepared/checkpoints"):
            (self.root / name).mkdir(parents=True)
        for name in ("baseline_model.py", "baseline_output.py", "baseline_result.py", "baseline_run.py"):
            (self.root / "tools" / name).write_text("")
        for name in (".work/baseline/prepared/provenance.json", ".work/prepared/checkpoints/provenance.json",
                     "artifacts/checkpoints.selection.json", "requirements/inference.lock"):
            (self.root / name).write_text("fixture")
        self.revision = "b" * 40
        self.environment = {**os.environ,
            "NMRNET_LABEL_ATOL_INPUT": "0.001", "NMRNET_ATOL_INPUT": "1e-6", "NMRNET_RTOL_INPUT": "0",
            "NMRNET_RUN_ID_INPUT": "reference-1",
            "BASH_FUNC_git%%": self.git_function}

    def command(self, *, print_command=True, environment=None):
        args = ["bash", str(self.root / "tools/baseline.sh")]
        if print_command:
            args.append("--print")
        return subprocess.run([*args, "run"], env=self.environment if environment is None else environment,
                              text=True, capture_output=True)

    def run_arguments(self):
        result = self.command()
        self.assertEqual(result.returncode, 0, result.stderr)
        return decode_command(result.stdout)

    def test_run_reads_exact_inputs_and_writes_only_runs(self):
        args = self.run_arguments()
        mounts = [args[index + 1] for index, item in enumerate(args) if item == "--mount"]
        readonly = {
            **{f"tools/{name}.py": f"/workspace/tools/{name}.py" for name in
               ("artifacts", "baseline_prepare", "baseline_model", "baseline_output", "baseline_result", "baseline_run")},
            "artifacts/release.json": "/input/release.json",
            "artifacts/checkpoints.selection.json": "/input/checkpoints.selection.json",
            "requirements/inference.lock": "/input/inference.lock",
            "requirements/unicore-source.json": "/input/unicore-source.json",
            ".work/baseline/prepared": "/prepared",
            ".work/prepared/checkpoints": "/checkpoints",
        }
        self.assertEqual(set(mounts), {
            f"type=bind,src={self.root / source},dst={destination},readonly"
            for source, destination in readonly.items()
        } | {f"type=bind,src={self.root / '.work/baseline/runs'},dst=/runs"})
        for flag, value in (("--network", "none"), ("--pull", "never"), ("--memory", "4g"),
                            ("--memory-swap", "4g"), ("--cpus", "2"), ("--pids-limit", "64"),
                            ("--output", "/runs/reference-1"), ("--label-atol", "0.001"),
                            ("--atol", "1e-6"), ("--rtol", "0"), ("--upstream-revision", self.revision),
                            ("--source", "/input/unicore-source.json")):
            self.assertEqual(args[args.index(flag) + 1], value)
        self.assertIn("--read-only", args)
        self.assertIn("no-new-privileges:true", args)
        self.assertNotIn("--gpus", args)
        self.assertEqual(args[args.index("--repository-revision") + 1], self.repository_revision)
        sources = [args[index + 1] for index, item in enumerate(args) if item == "--tool-source"]
        self.assertEqual(set(sources), {name for name in readonly if name.startswith("tools/")})
        self.assertFalse((self.root / ".work/baseline/runs").exists())

    def test_missing_tolerances_and_unsafe_run_id_reject(self):
        for name in ("LABEL_ATOL", "ATOL", "RTOL", "RUN_ID"):
            environment = dict(self.environment)
            environment.pop("NMRNET_" + name + "_INPUT")
            environment[name] = "0.001"  # Ordinary ambient variables do not authorize a choice.
            self.assertNotEqual(self.command(environment=environment).returncode, 0)
        for run_id in ("../escape", ".", "a/b", "", "x\ny"):
            environment = {**self.environment, "NMRNET_RUN_ID_INPUT": run_id}
            self.assertNotEqual(self.command(environment=environment).returncode, 0)

    def test_run_does_not_need_original_preparation_data(self):
        for name in ("data", "weight"):
            (self.root / ".work/prepared" / name / "provenance.json").unlink()
            (self.root / ".work/prepared" / name).rmdir()
        self.assertEqual(self.command().returncode, 0)

    def test_existing_run_and_symlinked_runs_parent_reject(self):
        runs = self.root / ".work/baseline/runs"
        runs.mkdir()
        (runs / "reference-1").mkdir()
        self.assertNotEqual(self.command().returncode, 0)
        (runs / "reference-1").rmdir()
        runs.rmdir()
        runs.symlink_to(self.root / "elsewhere")
        self.assertNotEqual(self.command().returncode, 0)

    def test_wrong_image_revision_rejects_before_run_or_output_creation(self):
        environment = {**self.environment, "BASH_FUNC_env%%": '() { echo "' + "c" * 40 + '"; }'}
        result = self.command(print_command=False, environment=environment)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("revision", result.stderr)
        self.assertFalse((self.root / ".work/baseline/runs").exists())

    def test_run_checks_both_embedded_inputs_before_scientific_imports(self):
        assert_image_admission(self, self.run_arguments(), "tools.baseline_run")


if __name__ == "__main__":
    unittest.main()
