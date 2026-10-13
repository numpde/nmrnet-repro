"""Retain independently executed H/C references and verify their repeatability."""

import argparse
import contextlib
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import platform
import re
import resource
import subprocess
import sys
import time

from tools import artifacts, baseline_model, baseline_output, baseline_prepare, baseline_result


def write_json(path, value):
    """Write owned evidence exclusively; a failed attempt never overwrites it."""
    encoded = json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n"
    with path.open("x") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())


def runtime_facts():
    facts = {"system": platform.system(), "machine": platform.machine(),
             "python": platform.python_version(), "logical_cpu_count": os.cpu_count(),
             "cpu_affinity_count": len(os.sched_getaffinity(0)),
             "thread_environment": {name: os.environ.get(name) for name in
                                    ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS")}}
    cpuinfo = Path("/proc/cpuinfo")
    if cpuinfo.is_file():
        facts["cpu_models"] = sorted({line.split(":", 1)[1].strip()
                                      for line in cpuinfo.read_text().splitlines()
                                      if line.startswith("model name")})
    facts["cgroup_v2"] = {name: (Path("/sys/fs/cgroup") / name).read_text().strip()
                          for name in ("cpu.max", "memory.max", "memory.swap.max", "pids.max")
                          if (Path("/sys/fs/cgroup") / name).is_file()}
    return facts


def run(*, prepared, checkpoint_root, declaration_path, selection_path, lock_path, source_path,
        output, user_dir, image_id, upstream_revision, label_atol, atol, rtol):
    """Run inside the offline inference container, retaining every failed attempt.

    The caller owns containment and verification of the supplied immutable image
    and source IDs. All inputs must be read-only mounts, and output must be a new
    directory. Only complete.json establishes execution/verification completion;
    scientific qualification still depends on the explicitly retained reviews.
    """
    for name, value in (("label_atol", label_atol), ("atol", atol), ("rtol", rtol)):
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError(f"Cannot run baseline: {name} must be an explicit finite nonnegative number")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise ValueError("Cannot run baseline: image_id must be an immutable sha256 image ID")
    if not re.fullmatch(r"[0-9a-f]{40}", upstream_revision):
        raise ValueError("Cannot run baseline: upstream_revision must be a full Git commit ID")

    output.mkdir()
    started = time.monotonic()
    report = {"started_utc": datetime.now(timezone.utc).isoformat(),
              "image_id": image_id, "upstream_revision": upstream_revision,
              "runtime_identity_source": "caller", "runtime": runtime_facts(),
              "tolerances": {"label_atol": label_atol, "atol": atol, "rtol": rtol},
              "completion_receipt": "complete.json"}
    write_json(output / "attempt.json", report)
    report["inputs"] = {name: {"path": str(path), **baseline_prepare.file_identity(path)}
                        for name, path in (("prepared_receipt", prepared / "provenance.json"),
                                           ("release_declaration", declaration_path),
                                           ("checkpoint_selection", selection_path),
                                           ("inference_lock", lock_path),
                                           ("unicore_source", source_path))}
    write_json(output / "inputs.json", report["inputs"])
    preflights = {}
    report["preflights"] = {}
    report["runs"] = {}
    report["comparisons"] = {}
    pending = set()
    # Qualify both models before spending time on any reference execution.
    for element in ("H", "C"):
        directory = output / element
        directory.mkdir()
        with (directory / "preflight.log").open("x") as log:
            with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
                preflights[element] = baseline_model.preflight(
                    prepared, checkpoint_root, declaration_path, selection_path,
                    element, directory / "run-1", user_dir)
        preflight_path = directory / "preflight.json"
        write_json(preflight_path, preflights[element])
        report["preflights"][element] = {"path": str(preflight_path.relative_to(output)),
                                         **baseline_prepare.file_identity(preflight_path)}
        pending.update(preflights[element]["pending_review"])

    for element in ("H", "C"):
        verified = []
        report["runs"][element] = []
        for repetition in (1, 2):
            # infer.main writes <checkpoint-parent>_<subset>.out.pkl with "wb".
            # Separate result directories preserve each independent execution.
            # https://github.com/numpde/fork-of-NMRNetplusplus/blob/3ba8b9c2f3d0a390d00685d238618345c6ab24d7/uninmr/infer.py#L85-L118
            directory = output / element / f"run-{repetition}"
            directory.mkdir()
            argv = list(preflights[element]["argv"])
            argv[argv.index("--results-path") + 1] = str(directory)
            command = [sys.executable, "-B", "-m", "uninmr.infer", *argv]
            write_json(directory / "invocation.json", {"command": command})
            inference_started = time.monotonic()
            log_path = directory / "inference.log"
            with log_path.open("x") as log:
                process = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=False)
            execution = {"returncode": process.returncode,
                         "elapsed_seconds": time.monotonic() - inference_started}
            write_json(directory / "execution.json", execution)
            if process.returncode != 0:
                raise RuntimeError(f"Cannot finish {element} baseline run {repetition}: upstream exit code {process.returncode}; retained evidence in {str(directory)!r}")
            checkpoint = Path(argv[argv.index("--path") + 1])
            subset = argv[argv.index("--valid-subset") + 1]
            upstream_output = directory / f"{checkpoint.parent.name}_{subset}.out.pkl"
            # Upstream iteration ignores invalid inputs; zero exit status alone
            # cannot establish that every prepared record produced predictions.
            # https://github.com/numpde/fork-of-NMRNetplusplus/blob/3ba8b9c2f3d0a390d00685d238618345c6ab24d7/uninmr/infer.py#L92-L118
            result = baseline_output.inspect_output(prepared, element, upstream_output, label_atol=label_atol)
            verification_path = directory / "verified.json"
            write_json(verification_path, result)
            verified.append(result["predictions"])
            report["runs"][element].append({"directory": str(directory.relative_to(output)),
                "command": command, **execution,
                "log": baseline_prepare.file_identity(log_path),
                "upstream_output": baseline_prepare.file_identity(upstream_output),
                "verification": baseline_prepare.file_identity(verification_path)})
        comparison = baseline_result.compare_runs(verified[0], verified[1], atol=atol, rtol=rtol)
        write_json(output / element / "comparison.json", comparison)
        report["comparisons"][element] = comparison

    report["pending_review"] = sorted(pending)
    report["elapsed_seconds"] = time.monotonic() - started
    report["finished_utc"] = datetime.now(timezone.utc).isoformat()
    report["resources"] = {
        "runner_max_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "children_max_rss_kib": resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,
        "scope": "Linux process lifetime high-water marks; children value is the maximum across waited-for child processes, not per-run memory or a sum",
    }
    report["status"] = "execution_and_repeatability_verified"
    # Publish the success marker only after its complete bytes reach disk. A
    # crash or exception leaves the attempt and partial evidence without it.
    pending_receipt = output / ".complete.json.pending"
    write_json(pending_receipt, report)
    os.link(pending_receipt, output / "complete.json")
    pending_receipt.unlink()
    artifacts.fsync_directory(output)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("prepared", "checkpoint-root", "declaration", "selection", "lock", "source", "output", "user-dir"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--image-id", required=True)
    parser.add_argument("--upstream-revision", required=True)
    for name in ("label-atol", "atol", "rtol"):
        parser.add_argument("--" + name, type=float, required=True)
    args = parser.parse_args()
    report = run(prepared=args.prepared, checkpoint_root=args.checkpoint_root,
                 declaration_path=args.declaration, selection_path=args.selection, lock_path=args.lock,
                 source_path=args.source,
                 output=args.output, user_dir=args.user_dir, image_id=args.image_id,
                 upstream_revision=args.upstream_revision, label_atol=args.label_atol,
                 atol=args.atol, rtol=args.rtol)
    print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
