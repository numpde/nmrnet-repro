"""Qualify selected release models through the pinned upstream inference stack."""

from tools.container_guard import require_container

require_container()

import argparse
import json
import os
import sys
from pathlib import Path, PurePosixPath

from nmrnet_inference.model import MODEL_SETTINGS, require_cpu_float32_model
from tools import artifacts, baseline_prepare

# Task/loss/architecture, selected atom, Gaussian kernel and inference split
# follow the upstream invocation:
# https://github.com/numpde/fork-of-NMRNetplusplus/blob/3ba8b9c2f3d0a390d00685d238618345c6ab24d7/script/infer.sh
# CPU float32, batch size 1 and zero workers are local bounded-baseline choices;
# infer.main honors --cpu and only converts to half precision when --fp16 is set.
# https://github.com/numpde/fork-of-NMRNetplusplus/blob/3ba8b9c2f3d0a390d00685d238618345c6ab24d7/uninmr/infer.py#L25-L55
# These fixed upstream ABI expectations belong to model qualification; exposing
# them as settings would let a changed invocation qualify a different contract.
# repo-check: allow-module-constant
CONTRACT = {
    **MODEL_SETTINGS,
    "task": "uninmr",
    "loss": "atom_regloss_mae",
    "split_mode": "infer",
    "valid_subset": "valid",
    "cpu": True,
    "fp16": False,
    "batch_size": 1,
    "num_workers": 0,
    "required_batch_size_multiple": 1,
    "seed": 42,
    "remove_hydrogen": False,
    "conformer_augmentation": False,
    "has_matid": False,
    "unlabeled_data": None,
    "distributed_world_size": 1,
}


def model_artifacts(root, archive, selection, element):
    """Resolve one selected configuration and recheck its bytes before loading.

    Archive extraction owns release verification. This check binds each consumed
    member to that receipt; the execution lane must mount this tree read-only.
    Names establish the declared pairing, not scientific proof of its contents.
    """
    if element not in ("H", "C"):
        raise ValueError(f"Cannot qualify model: element {element!r} must be H or C")
    names = [artifacts.safe_member_name(name) for name in selection["members"]]
    if len(set(names)) != len(names):
        raise ValueError("Cannot qualify checkpoint configuration: selection contains duplicate members")
    checkpoints = []
    for name in names:
        path = PurePosixPath(name)
        if (
            len(path.parts) == 4
            and path.parts[0] == "checkpoints"
            and path.parts[1].startswith(f"scratch_{element}_")
            and path.parts[2] == "cv_seed_42_fold_0"
            and path.name == "checkpoint_last.pt"
        ):
            checkpoints.append(path)
    if len(checkpoints) != 1:
        raise ValueError(
            f"Cannot qualify {element} checkpoint: expected one selected scratch configuration at fold 0, found {len(checkpoints)}"
        )
    checkpoint = checkpoints[0]
    configuration = checkpoint.parent.parent
    members = {
        "checkpoint": checkpoint,
        "scaler": configuration / "target_scaler.ss",
        "infer_log": configuration / "infer.log",
        "finetune_log": configuration / "finetune.log",
    }
    paths, identities = {}, {}
    for role, member in members.items():
        if str(member) not in names:
            raise ValueError(f"Cannot qualify {element} model: selected configuration is missing paired {role}: {str(member)!r}")
        paths[role], identities[role] = baseline_prepare.verified_member(
            root, archive, {"members": [str(member)], "max_bytes": selection["max_bytes"]}
        )
    if not (sum(item["size"] for item in identities.values()) <= selection["max_bytes"]):
        raise ValueError("Cannot qualify model: selected configuration exceeds max_bytes")
    return paths, identities


def inference_argv(prepared, element, paths, bounds, results, user_dir):
    """Return the same explicit invocation for preflight and upstream inference."""
    if element not in ("H", "C"):
        raise ValueError(f"Cannot build inference arguments: element {element!r} must be H or C")
    for name, minimum in (("max_atoms", 1), ("max_tokens", 3)):
        if not (type(bounds[name]) is int and minimum <= bounds[name]):
            raise ValueError(f"Cannot build inference arguments: {name} must be an integer >= {minimum}")
    return [
        str(prepared / element),
        "--user-dir",
        str(user_dir),
        "--task",
        "uninmr",
        "--loss",
        "atom_regloss_mae",
        "--arch",
        "unimol_large",
        "--selected-atom",
        element,
        "--classification-head-name",
        "nmr_head",
        "--num-classes",
        "1",
        "--gaussian-kernel",
        "--atom-descriptor",
        "0",
        "--split-mode",
        "infer",
        "--dict-name",
        "dict.txt",
        "--saved-dir",
        str(paths["scaler"].parent),
        "--path",
        str(paths["checkpoint"]),
        "--valid-subset",
        "valid",
        "--cpu",
        "--batch-size",
        "1",
        "--num-workers",
        "0",
        "--required-batch-size-multiple",
        "1",
        "--seed",
        "42",
        "--distributed-world-size",
        "1",
        "--max-atoms",
        str(bounds["max_atoms"]),
        "--max-seq-len",
        str(bounds["max_tokens"]),
        "--log-format",
        "simple",
        "--results-path",
        str(results),
    ]


def validate_args(args, element, bounds):
    expected = {**CONTRACT, "selected_atom": element, "max_atoms": bounds["max_atoms"], "max_seq_len": bounds["max_tokens"]}
    for name, value in expected.items():
        if getattr(args, name, object()) != value:
            raise ValueError(f"Cannot qualify model: resolved {name} must equal {value!r}")


def validate_dictionary(task, expected):
    if (
        list(task.dictionary.symbols) != expected["symbols"]
        or {"symbol": "[MASK]", "index": task.mask_idx} != expected["task_special_token"]
    ):
        raise ValueError("Cannot qualify model: ordered task dictionary differs from the preparation receipt")


def validate_load(result):
    # Upstream inference uses strict=False; explicitly reject omitted/extra state
    # here so a successful invocation cannot silently qualify a partial model.
    # https://github.com/numpde/fork-of-NMRNetplusplus/blob/3ba8b9c2f3d0a390d00685d238618345c6ab24d7/uninmr/infer.py#L44-L49
    if not hasattr(result, "missing_keys") or not hasattr(result, "unexpected_keys"):
        raise ValueError("Cannot qualify checkpoint: upstream loader did not return missing/unexpected key evidence")
    report = {"missing_keys": list(result.missing_keys), "unexpected_keys": list(result.unexpected_keys)}
    if report["missing_keys"] or report["unexpected_keys"]:
        raise ValueError(f"Cannot qualify checkpoint: state differs from the constructed model: {report!r}")
    return report


def preflight(prepared, checkpoint_root, declaration_path, selection_path, element, results, user_dir):
    """Construct/load the real model without running inference or fitting a scaler.

    All inputs must be read-only container mounts. Checkpoint and scaler loaders
    deserialize trusted release objects only after their receipt checks. The
    report deliberately leaves release log/configuration and target-unit review
    pending; load compatibility alone does not establish those scientific facts.
    """
    declaration = artifacts.load_declaration(declaration_path)
    archive = next(item for item in declaration["files"] if item["name"] == "checkpoints.zip")
    selection = json.loads(selection_path.read_text())
    paths, identities = model_artifacts(checkpoint_root, archive, selection, element)
    with artifacts.regular_file(prepared / "provenance.json", os.O_RDONLY) as stream:
        receipt = json.load(stream)
    if receipt["record_id"] != declaration["record_id"] or receipt["doi"] != declaration["doi"]:
        raise ValueError("Cannot qualify model: preparation and checkpoint release identities differ")
    selected = receipt["elements"][element]
    for name in ("valid.lmdb", "valid_dbid.pkl", "dict.txt"):
        if baseline_prepare.file_identity(prepared / element / name) != selected["files"][name]:
            raise ValueError(f"Cannot qualify model: prepared {name} differs from its receipt")
    bounds = receipt["selection"]
    if not selected["records"] or any(
        record["atom_count"] > bounds["max_atoms"] or record["atom_count"] + 2 > bounds["max_tokens"]
        for record in selected["records"]
    ):
        raise ValueError("Cannot qualify model: prepared records are empty or exceed intact-record bounds")
    argv = inference_argv(prepared, element, paths, bounds, results, user_dir)

    # Import the scientific stack only after receipt checks; source/unit checks
    # must remain usable without loading those execution dependencies.
    import torch  # noqa: PLC0415
    from unicore import checkpoint_utils, options, tasks  # noqa: PLC0415

    parser = options.get_validation_parser()
    # The pinned root argparse factory already owns None for optional absence.
    # https://github.com/dptech-corp/Uni-Core/blob/ace6fae1c8479a9751f2bb1e1d6e4047427bc134/unicore/options.py#L159-L167
    parser.add_argument("--subset_name", type=str)
    options.add_model_args(parser)
    args = options.parse_args_and_arch(parser, input_args=argv)
    validate_args(args, element, bounds)
    state = checkpoint_utils.load_checkpoint_to_cpu(args.path)
    task = tasks.setup_task(args)
    validate_dictionary(task, receipt["dictionary"])
    if task.target_scaler.scaler is None:
        raise ValueError("Cannot qualify model: upstream did not load the paired target scaler")
    model = task.build_model(args)
    # Shape errors propagate from the real loader, just as in upstream inference.
    load_report = validate_load(model.load_state_dict(state["model"], strict=False))
    parameter_count = require_cpu_float32_model(model, torch.float32)
    loss = task.build_loss(args)
    loss.eval()
    return {
        "element": element,
        "argv": argv,
        "resolved_args": vars(args),
        "artifacts": identities,
        "dictionary": receipt["dictionary"],
        "checkpoint_load": load_report,
        "parameter_count": parameter_count,
        "device": "cpu",
        "dtype": "float32",
        "torch_version": torch.__version__,
        "model_class": type(model).__name__,
        "loss_class": type(loss).__name__,
        "scaler_class": type(task.target_scaler.scaler).__name__,
        "pending_review": ["release log/configuration agreement", "target units"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared", type=Path, required=True)
    parser.add_argument("--checkpoint-root", type=Path, required=True)
    parser.add_argument("--declaration", type=Path, required=True)
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--element", choices=("H", "C"), required=True)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--user-dir", type=Path, required=True)
    args = parser.parse_args()
    report = preflight(
        args.prepared, args.checkpoint_root, args.declaration, args.selection, args.element, args.results, args.user_dir
    )
    sys.stdout.write(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n")


if __name__ == "__main__":
    main()
