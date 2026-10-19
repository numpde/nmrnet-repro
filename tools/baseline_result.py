"""Align normalized upstream inference batches to verified prepared records."""

from tools.container_guard import require_container

require_container()

import hashlib
import math
import pickle


def _tolerance(value, name):
    """Require a finite, nonnegative built-in number without admitting booleans."""
    if not (type(value) in (int, float) and math.isfinite(value) and value >= 0):
        raise ValueError(f"Cannot verify baseline: {name} must be finite and nonnegative")


def _column(batch, field, batch_index):
    column = batch[field]
    if not isinstance(column, list) or any(not isinstance(row, list) or len(row) != 1 for row in column):
        raise ValueError(f"Cannot verify batch {batch_index}: {field} must have shape [N, 1]")
    return [row[0] for row in column]


def validate_batches(
    batches, records, prepared_records, *, element, dictionary_symbols, bos_index, eos_index, pad_index, label_atol
):
    """Validate one run, returning predictions in source record and atom order.

    Batches retain upstream shapes but use Python lists/scalars. Prepared records
    are ordered (key, original pickle bytes) pairs bound to preparation receipts;
    only verified release pickles may reach this function. The caller normalizes
    actual NumPy/Torch outputs in its inference environment.

    Upstream saves selected, inverse-scaled predictions: neither operation is
    repeated here. Its labels take a float32 scaler round trip, so the caller
    supplies an explicit allowance separately from repeated-run tolerances.
    """
    # AtomRegMSELoss.forward flattens token/mask columns and emits inverse-scaled
    # float32 prediction/target columns; UniMatModel.forward selects atoms first.
    # These define the shapes and prevent a second mask or inverse transform.
    # https://github.com/numpde/fork-of-NMRNetplusplus/blob/3ba8b9c2f3d0a390d00685d238618345c6ab24d7/uninmr/losses/atom_regloss.py#L35-L81
    # https://github.com/numpde/fork-of-NMRNetplusplus/blob/3ba8b9c2f3d0a390d00685d238618345c6ab24d7/uninmr/models/unimat.py#L275-L284
    # Labels are scaled before ToTorchDataset casts to float32, accounting for
    # the separate caller-specified label allowance on the inverse-scaled output.
    # https://github.com/numpde/fork-of-NMRNetplusplus/blob/3ba8b9c2f3d0a390d00685d238618345c6ab24d7/uninmr/data/normalize_dataset.py#L31-L38
    # https://github.com/numpde/fork-of-NMRNetplusplus/blob/3ba8b9c2f3d0a390d00685d238618345c6ab24d7/uninmr/tasks/uninmr.py#L381-L385
    _tolerance(label_atol, "label_atol")
    if element not in ("H", "C"):
        raise ValueError(f"Cannot verify baseline: element {element!r} must be H or C")
    if not records or len(records) != len(prepared_records):
        raise ValueError("Cannot verify baseline: receipt and prepared records must have the same nonzero count")
    if len(set(dictionary_symbols)) != len(dictionary_symbols):
        raise ValueError("Cannot verify baseline: dictionary must not contain duplicate symbols")
    for name, index in (("bos_index", bos_index), ("eos_index", eos_index), ("pad_index", pad_index)):
        if type(index) is not int or not 0 <= index < len(dictionary_symbols):
            raise ValueError(f"Cannot verify baseline: {name} is outside the ordered dictionary")
    if pad_index != 0:
        raise ValueError("Cannot verify baseline: upstream selection masks require dictionary pad_index=0")
    token_indices = {symbol: index for index, symbol in enumerate(dictionary_symbols)}
    expected = []
    keys = set()
    # The count guard above establishes complete receipt/source pairing.
    for index, (receipt, (key, raw)) in enumerate(zip(records, prepared_records, strict=True)):
        if (
            receipt["dataset_index"] != index
            or receipt["key_hex"] != key.hex()
            or receipt["raw_sha256"] != hashlib.sha256(raw).hexdigest()
            or key in keys
        ):
            raise ValueError(f"Cannot verify record {index}: prepared identity differs from its ordered receipt")
        keys.add(key)
        source = pickle.loads(raw)
        atoms = source["atoms"]
        indices = [i for i, atom in enumerate(atoms) if atom == element and source["atoms_target_mask"][i] == 1]
        labels = [float(source["atoms_target"][i]) for i in indices]
        if (
            len(atoms) != receipt["atom_count"]
            or not indices
            or indices != receipt["selected_atom_indices"]
            or labels != receipt["selected_labels"]
            or not all(map(math.isfinite, labels))
        ):
            raise ValueError(f"Cannot verify record {index}: source atom/label order differs from its receipt")
        if any(atom not in token_indices for atom in atoms):
            raise ValueError(f"Cannot verify record {index}: an atom has no dedicated dictionary token")
        tokens = [bos_index] + [token_indices[atom] for atom in atoms] + [eos_index]
        selected = set(indices)
        mask = [0] + [int(i in selected) for i in range(len(atoms))] + [0]
        expected.append((tokens, mask, indices, labels))

    aligned = {}
    max_label_difference = 0.0
    for batch_index, batch in enumerate(batches):
        # With --has-matid disabled, IndexDataset emits the filtered dataset
        # ordinal, not the original LMDB key; preparation retains that mapping.
        # https://github.com/numpde/fork-of-NMRNetplusplus/blob/3ba8b9c2f3d0a390d00685d238618345c6ab24d7/uninmr/data/key_dataset.py#L43-L52
        # https://github.com/numpde/fork-of-NMRNetplusplus/blob/3ba8b9c2f3d0a390d00685d238618345c6ab24d7/uninmr/tasks/uninmr.py#L337-L340
        ids = batch["matid"]
        if not isinstance(ids, list) or not ids or batch["num_task"] != 1:
            raise ValueError(f"Cannot verify batch {batch_index}: nonempty matid and num_task=1 are required")
        tokens = _column(batch, "src_token", batch_index)
        masks = _column(batch, "select_atom", batch_index)
        predictions = _column(batch, "predict", batch_index)
        targets = _column(batch, "target", batch_index)
        if len(tokens) != len(masks) or len(tokens) % len(ids):
            raise ValueError(f"Cannot verify batch {batch_index}: flattened token/mask rows do not align with matid")
        if any(type(value) is not int for value in tokens + masks):
            raise ValueError(f"Cannot verify batch {batch_index}: token and mask entries must be integers")
        if (
            len(predictions) != len(targets)
            or batch["sample_size"] != len(predictions)
            or len(predictions) != sum(value == 1 for value in masks)
        ):
            raise ValueError(f"Cannot verify batch {batch_index}: selected prediction, target and sample counts differ")
        if any(type(value) not in (int, float) or not math.isfinite(value) for value in predictions + targets):
            raise ValueError(f"Cannot verify batch {batch_index}: predictions and targets must be finite numbers")
        width = len(tokens) // len(ids)
        offset = 0
        for row, index in enumerate(ids):
            if type(index) is not int or not 0 <= index < len(records):
                raise ValueError(f"Cannot verify batch {batch_index}: matid {index!r} is not a prepared record index")
            if index in aligned:
                raise ValueError(f"Cannot verify batch {batch_index}: duplicate record {index}")
            source_tokens, source_mask, indices, labels = expected[index]
            padding = width - len(source_tokens)
            start = row * width
            if not (
                padding >= 0
                and tokens[start : start + width] == source_tokens + [pad_index] * padding
                and masks[start : start + width] == source_mask + [0] * padding
            ):
                raise ValueError(f"Cannot verify record {index}: emitted tokens or selection mask differ from source atom order")
            rows = []
            # Labels were collected once for every selected source index.
            for atom_index, label in zip(indices, labels, strict=True):
                difference = abs(targets[offset] - label)
                max_label_difference = max(max_label_difference, difference)
                # Finite operands can overflow to infinity, but not NaN.
                if not (difference <= label_atol):
                    raise ValueError(
                        f"Cannot verify record {index} atom {atom_index}: emitted label differs from source by {difference}, exceeding label_atol={label_atol}"
                    )
                rows.append(
                    {
                        "dataset_index": index,
                        "key_hex": records[index]["key_hex"],
                        "atom_index": atom_index,
                        "token": source_tokens[atom_index + 1],
                        "target": label,
                        "prediction": predictions[offset],
                    }
                )
                offset += 1
            aligned[index] = rows
    missing = sorted(set(range(len(records))) - aligned.keys())
    if missing:
        raise ValueError(f"Cannot verify baseline: missing prepared records {missing}")
    return {
        "predictions": [row for index in range(len(records)) for row in aligned[index]],
        "max_label_abs_difference": max_label_difference,
        "label_atol": label_atol,
    }


def compare_runs(reference, repeated, *, atol, rtol):
    """Compare validated runs per atom using abs(delta) <= atol + rtol*abs(reference).

    Callers retain and hash each independently produced output before validation;
    these normalized rows alone cannot establish that two runs occurred.
    """
    _tolerance(atol, "atol")
    _tolerance(rtol, "rtol")
    if not reference or len(reference) != len(repeated):
        raise ValueError("Cannot compare baseline runs: prediction counts must be equal and nonzero")
    maximum = 0.0
    # The count guard above establishes complete repeated-run pairing.
    for original, current in zip(reference, repeated, strict=True):
        if any(original[field] != current[field] for field in ("dataset_index", "key_hex", "atom_index", "token", "target")):
            raise ValueError("Cannot compare baseline runs: record/atom alignment or source labels differ")
        left, right = original["prediction"], current["prediction"]
        if not math.isfinite(left) or not math.isfinite(right):
            raise ValueError("Cannot compare baseline runs: every prediction must be finite")
        difference = abs(right - left)
        maximum = max(maximum, difference)
        # Validated predictions and nonnegative finite tolerances may overflow
        # to infinity; neither side can become NaN, so the inverse is exact.
        if not (difference <= atol + rtol * abs(left)):
            raise ValueError(
                f"Cannot reproduce record {original['dataset_index']} atom {original['atom_index']}: absolute difference {difference} exceeds atol={atol} plus rtol={rtol} * abs(reference={left})"
            )
    return {"prediction_count": len(reference), "max_abs_difference": maximum, "atol": atol, "rtol": rtol}
