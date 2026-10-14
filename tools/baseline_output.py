"""Verify one retained upstream output and print source-aligned predictions as JSON."""

from tools.container_guard import require_container

require_container()

import argparse
import hashlib
import json
import os
import pickle
import sys
from pathlib import Path

from tools import artifacts, baseline_prepare, baseline_result


def _plain(value):
    if type(value) in (int, float, bool):
        return value
    if isinstance(value, list):
        return [_plain(item) for item in value]
    # Tensor storage is detached and moved before conversion; NumPy arrays and
    # scalars need only tolist(). Pickle imports these dependencies when needed.
    if hasattr(value, "detach"):
        value = value.detach().cpu()
    if hasattr(value, "tolist"):
        return _plain(value.tolist())
    raise TypeError(f"Cannot normalize upstream output: {type(value).__name__} is not a numeric scalar, array or tensor")


def normalize_batches(batches):
    """Preserve upstream shapes and normalize only fields consumed by validation."""
    # Consume the logging_output fields defined by AtomRegMSELoss.forward;
    # conversion changes tensor/array containers without changing their shape.
    # https://github.com/numpde/fork-of-NMRNetplusplus/blob/3ba8b9c2f3d0a390d00685d238618345c6ab24d7/uninmr/losses/atom_regloss.py#L73-L81
    if not isinstance(batches, list):
        raise ValueError("Cannot verify upstream output: the pickle must contain a list of batches")
    fields = ("matid", "num_task", "sample_size", "src_token", "select_atom", "predict", "target")
    normalized = []
    for index, batch in enumerate(batches):
        if not isinstance(batch, dict) or any(field not in batch for field in fields):
            raise ValueError(f"Cannot verify upstream batch {index}: required prediction or alignment fields are missing")
        normalized.append({field: _plain(batch[field]) for field in fields})
    return normalized


def inspect_output(prepared, element, upstream_output, *, label_atol):
    """Read verified preparation inputs and a trusted retained inference pickle.

    The execution lane must mount inputs read-only, including LMDB files that
    are reopened after hashing. Pickles execute code while loading: this entry
    point is for retained outputs from the pinned inference lane, not arbitrary
    third-party result files. Local hashes identify bytes, not their authenticity.
    """
    if element not in ("H", "C"):
        raise ValueError(f"Cannot inspect baseline output: element {element!r} must be H or C")
    with artifacts.regular_file(prepared / "provenance.json", os.O_RDONLY) as stream:
        receipt_bytes = stream.read()
    receipt = json.loads(receipt_bytes)
    selected = receipt["elements"][element]
    split = prepared / element
    for name in ("valid.lmdb", "valid_dbid.pkl", "dict.txt"):
        if baseline_prepare.file_identity(split / name) != selected["files"][name]:
            raise ValueError(f"Cannot inspect {element} baseline output: prepared {name} differs from its receipt")

    with artifacts.regular_file(split / "valid_dbid.pkl", os.O_RDONLY) as stream:
        keys = pickle.load(stream)
    records = selected["records"]
    if keys != [bytes.fromhex(record["key_hex"]) for record in records]:
        raise ValueError(f"Cannot inspect {element} baseline output: companion keys differ from the receipt's record order")
    if selected["record_count"] != len(records):
        raise ValueError(f"Cannot inspect {element} baseline output: receipt record count differs from its records")

    # Receipt checks precede scientific imports so source/unit checks remain
    # usable without loading the inference dependencies.
    import lmdb  # noqa: PLC0415
    from unicore.data import Dictionary  # noqa: PLC0415

    dictionary = Dictionary.load(str(split / "dict.txt"))
    expected_dictionary = receipt["dictionary"]
    if list(dictionary.symbols) != expected_dictionary["symbols_before_task"]:
        raise ValueError(f"Cannot inspect {element} baseline output: ordered dictionary differs from its preparation receipt")
    # Reconstruct UniNMRTask's dictionary extension before checking token IDs.
    # https://github.com/numpde/fork-of-NMRNetplusplus/blob/3ba8b9c2f3d0a390d00685d238618345c6ab24d7/uninmr/tasks/uninmr.py#L174-L180
    mask_index = dictionary.add_symbol("[MASK]", is_special=True)
    if (
        list(dictionary.symbols) != expected_dictionary["symbols"]
        or {"symbol": "[MASK]", "index": mask_index} != expected_dictionary["task_special_token"]
    ):
        raise ValueError(
            f"Cannot inspect {element} baseline output: task dictionary mapping differs from its preparation receipt"
        )

    prepared_records = []
    with (
        lmdb.open(
            str(split / "valid.lmdb"), subdir=False, readonly=True, lock=False, readahead=False, meminit=False
        ) as environment,
        environment.begin() as transaction,
    ):
        if transaction.stat()["entries"] != len(keys):
            raise ValueError(f"Cannot inspect {element} baseline output: LMDB entry count differs from its companion keys")
        for key in keys:
            raw = transaction.get(key)
            if raw is None:
                raise ValueError(f"Cannot inspect {element} baseline output: source record {key.hex()!r} is missing from LMDB")
            prepared_records.append((key, raw))

    with artifacts.regular_file(upstream_output, os.O_RDONLY) as stream:
        output_bytes = stream.read()
    batches = normalize_batches(pickle.loads(output_bytes))
    result = baseline_result.validate_batches(
        batches,
        records,
        prepared_records,
        element=element,
        dictionary_symbols=list(dictionary.symbols),
        bos_index=dictionary.bos(),
        eos_index=dictionary.eos(),
        pad_index=dictionary.pad(),
        label_atol=label_atol,
    )
    if len(result["predictions"]) != selected["selected_atom_count"]:
        raise ValueError(f"Cannot inspect {element} baseline output: selected atom count differs from its preparation receipt")
    return {
        "element": element,
        "prepared_receipt": {
            "path": str(prepared / "provenance.json"),
            "size": len(receipt_bytes),
            "sha256": hashlib.sha256(receipt_bytes).hexdigest(),
        },
        "upstream_output": {
            "path": str(upstream_output),
            "size": len(output_bytes),
            "sha256": hashlib.sha256(output_bytes).hexdigest(),
        },
        **result,
    }


def main():
    """Write one verified report as JSON followed by a newline."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared", type=Path, required=True)
    parser.add_argument("--element", choices=("H", "C"), required=True)
    parser.add_argument("--upstream-output", type=Path, required=True)
    parser.add_argument(
        "--label-atol",
        type=float,
        required=True,
        help="Explicit absolute allowance for the saved labels' float32 scaler round trip",
    )
    args = parser.parse_args()
    report = inspect_output(args.prepared, args.element, args.upstream_output, label_atol=args.label_atol)
    sys.stdout.write(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n")


if __name__ == "__main__":
    main()
