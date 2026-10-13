"""Select intact reference records from already verified release data."""

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import pickle
import re
import shutil
import tempfile

from tools import artifacts


def select_records(records, *, element, dictionary_tokens, count, max_atoms, max_tokens):
    """Take the first eligible records, retaining their original keys and bytes.

    The caller owns release verification before yielding pickled LMDB values.
    Iteration order determines reference order. Limits include both boundary
    tokens; no coordinate, atom, mask, or target transformation occurs here.
    Receipts map the derived dataset indices back to original atom positions.
    """
    if element not in ("H", "C"):
        raise ValueError(f"Cannot select baseline records: element {element!r} must be H or C")
    for name, value, minimum in (("count", count, 1), ("max_atoms", max_atoms, 1),
                                 ("max_tokens", max_tokens, 3)):
        if type(value) is not int or value < minimum:
            raise ValueError(f"Cannot select baseline records: {name} must be an integer >= {minimum}")

    vocabulary = set(dictionary_tokens)
    selected = []
    receipt = []
    seen_keys = set()
    for key, raw in records:
        if key in seen_keys:
            raise ValueError(f"Cannot select baseline records: duplicate source key {key.hex()!r}")
        seen_keys.add(key)
        record = pickle.loads(raw)
        atoms = record["atoms"]
        atom_count = len(atoms)
        for field in ("coordinates", "atoms_target", "atoms_target_mask"):
            if len(record[field]) != atom_count:
                raise ValueError(
                    f"Cannot select record {key.hex()!r}: {field} must align with {atom_count} atoms"
                )
        coordinates = record["coordinates"]
        targets = record["atoms_target"]
        mask = record["atoms_target_mask"]
        if any(len(row) != 3 for row in coordinates):
            raise ValueError(f"Cannot select record {key.hex()!r}: coordinates must have three values per atom")
        if any(value not in (0, 1) for value in mask):
            raise ValueError(f"Cannot select record {key.hex()!r}: atoms_target_mask must contain only 0 or 1")
        for atom in atoms:
            if atom not in vocabulary:
                raise ValueError(
                    f"Cannot select record {key.hex()!r}: dictionary has no dedicated token for {atom!r}"
                )

        # Upstream CroppingDataset changes atoms/coordinates but leaves targets
        # and masks intact; reject oversized records to preserve atom alignment.
        # https://github.com/numpde/fork-of-NMRNetplusplus/blob/3ba8b9c2f3d0a390d00685d238618345c6ab24d7/uninmr/data/cropping_dataset.py#L27-L38
        if atom_count > max_atoms or atom_count + 2 > max_tokens:
            continue
        indices = [index for index, atom in enumerate(atoms)
                   if atom == element and mask[index] == 1]
        if len(indices) == 0:
            continue
        if not all(math.isfinite(value) for row in coordinates for value in row):
            continue
        if not all(math.isfinite(value) for value in targets):
            continue

        # SolventDataset distinguishes a missing key (0) from an unknown value
        # (3), including explicit None. Record its effective category unchanged.
        # https://github.com/numpde/fork-of-NMRNetplusplus/blob/3ba8b9c2f3d0a390d00685d238618345c6ab24d7/uninmr/data/key_dataset.py#L23-L40
        solvent_present = "nmr_solvent" in record
        solvent = record.get("nmr_solvent")
        category = {"CDCl3": 1, "DMSO-d6": 2}.get(solvent, 3) if solvent_present else 0
        receipt.append({
            "dataset_index": len(selected),
            "key_hex": key.hex(),
            "raw_sha256": hashlib.sha256(raw).hexdigest(),
            "atom_count": atom_count,
            "selected_atom_indices": indices,
            "selected_labels": [float(targets[index]) for index in indices],
            "solvent_present": solvent_present,
            "raw_solvent": solvent,
            "effective_solvent_category": category,
        })
        selected.append((key, raw))
        if len(selected) == count:
            return selected, receipt

    raise ValueError(
        f"Cannot select {element} baseline: requested {count} records, found {len(selected)} "
        f"with finite labels and coordinates within max_atoms={max_atoms}, max_tokens={max_tokens}"
    )


def file_identity(path):
    digest = hashlib.sha256()
    size = 0
    with artifacts.regular_file(path, os.O_RDONLY) as stream:
        while chunk := stream.read(artifacts.CHUNK_BYTES):
            size += len(chunk)
            digest.update(chunk)
    return {"size": size, "sha256": digest.hexdigest()}


def execution_identity(*, image_id, repository_revision, tool_sources):
    """Record mounted tool bytes separately from the caller's Git/image identities.

    tools/baseline.sh supplies this list from its read-only module mounts. HEAD
    identifies the parent revision; tool hashes also identify uncommitted edits.
    The image owns the scientific dependencies and upstream model source.
    """
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise ValueError("Cannot record baseline provenance: image_id must be an immutable sha256 image ID")
    if not re.fullmatch(r"[0-9a-f]{40}", repository_revision):
        raise ValueError("Cannot record baseline provenance: repository_revision must be a full Git commit ID")
    return {"image_id": image_id, "repository_revision": repository_revision,
            "runtime_identity_source": "caller",
            "tool_sources": {f"tools/{path.name}": file_identity(path) for path in tool_sources}}


def verified_member(prepared, archive, selection):
    """Recheck an extracted member against the acquisition owner's receipt.

    The preparation lane must mount these source directories read-only. This
    binds extracted bytes to the checked archive receipt, without claiming that
    the locally computed member SHA-256 independently authenticates the release.
    """
    names = selection["members"]
    if not isinstance(names, list) or len(names) != 1:
        raise ValueError("Cannot prepare baseline: each source selection must name exactly one member")
    name = artifacts.safe_member_name(names[0])
    limit = selection["max_bytes"]
    if type(limit) is not int or limit <= 0:
        raise ValueError("Cannot prepare baseline: selection max_bytes must be a positive integer")
    provenance = json.loads((prepared / "provenance.json").read_text())
    if provenance["archive"] != archive:
        raise ValueError(f"Cannot prepare baseline: {archive['name']!r} disagrees with the extraction archive provenance")
    if name not in provenance["members"]:
        raise ValueError(f"Cannot prepare {name!r}: member is missing from extraction provenance")
    path = prepared / name
    if not path.resolve().is_relative_to(prepared.resolve()):
        raise ValueError(f"Cannot prepare {name!r}: member resolves outside its extracted directory")
    expected = provenance["members"][name]
    if path.stat().st_size > limit:
        raise ValueError(f"Cannot prepare {name!r}: size exceeds selection max_bytes={limit}")
    actual = file_identity(path)
    for field in ("size", "sha256"):
        if actual[field] != expected[field]:
            raise ValueError(f"Cannot prepare {name!r}: {field} differs from extraction provenance")
    return path, {"archive": archive, "archive_sha256": provenance["archive_sha256"],
                  "member": name, **actual}


def write_split(directory, selected, dictionary):
    """Write original LMDB values, then reopen both files to verify their mapping."""
    import lmdb

    directory.mkdir()
    database = directory / "valid.lmdb"
    keys = [key for key, _ in selected]
    map_size = (1 << 20) + 4 * sum(len(key) + len(raw) for key, raw in selected)
    with lmdb.open(str(database), subdir=False, map_size=map_size) as environment:
        with environment.begin(write=True) as transaction:
            for key, raw in selected:
                if not transaction.put(key, raw, overwrite=False):
                    raise ValueError(f"Cannot prepare {directory.name}: duplicate LMDB key {key.hex()!r}")
        environment.sync()
    (directory / "valid.lmdb-lock").unlink()
    # LMDBDataset consumes this ordered key cache or creates it beside the data.
    # Supply it now so inference can mount the prepared split read-only.
    # https://github.com/numpde/fork-of-NMRNetplusplus/blob/3ba8b9c2f3d0a390d00685d238618345c6ab24d7/uninmr/data/lmdb_dataset.py#L17-L33
    companion = directory / "valid_dbid.pkl"
    companion.write_bytes(pickle.dumps(keys, protocol=4))
    (directory / "dict.txt").write_bytes(dictionary)

    copied_keys = pickle.loads(companion.read_bytes())
    if copied_keys != keys:
        raise ValueError(f"Cannot prepare {directory.name}: valid_dbid.pkl changed record order")
    with lmdb.open(str(database), subdir=False, readonly=True, lock=False) as environment:
        with environment.begin() as transaction:
            if transaction.stat()["entries"] != len(keys):
                raise ValueError(f"Cannot prepare {directory.name}: copied LMDB record count differs")
            for key, original in selected:
                if transaction.get(key) != original:
                    raise ValueError(f"Cannot prepare {directory.name}: copied bytes differ for key {key.hex()!r}")
    return {name: file_identity(directory / name)
            for name in ("valid.lmdb", "valid_dbid.pkl", "dict.txt")}


def prepare(prepared_root, declaration_path, data_selection_path, dictionary_selection_path,
            output, *, image_id, repository_revision, tool_sources,
            count=2, max_atoms=128, max_tokens=130):
    """Publish both reference subsets only after every copy and identity check."""
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"Cannot prepare baseline: output {str(output)!r} already exists")
    execution = execution_identity(image_id=image_id, repository_revision=repository_revision,
                                   tool_sources=tool_sources)
    declaration = artifacts.load_declaration(declaration_path)
    archives = {item["name"]: item for item in declaration["files"]}
    sources = {}
    paths = {}
    for role, selection_path in (("data", data_selection_path),
                                 ("dictionary", dictionary_selection_path)):
        if not selection_path.name.endswith(".selection.json"):
            raise ValueError("Cannot prepare baseline: source declarations must be named <archive>.selection.json")
        stem = selection_path.name.removesuffix(".selection.json")
        archive_name = stem + ".zip"
        if archive_name not in archives:
            raise ValueError(f"Cannot prepare baseline: archive {archive_name!r} is not declared")
        selection = json.loads(selection_path.read_text())
        paths[role], sources[role] = verified_member(prepared_root / stem, archives[archive_name], selection)
    import lmdb
    from unicore.data import Dictionary

    dictionary_bytes = paths["dictionary"].read_bytes()
    dictionary = Dictionary.load(str(paths["dictionary"]))
    symbols_before_mask = list(dictionary.symbols)
    # Match UniNMRTask's dictionary extension so receipts use runtime indices.
    # https://github.com/numpde/fork-of-NMRNetplusplus/blob/3ba8b9c2f3d0a390d00685d238618345c6ab24d7/uninmr/tasks/uninmr.py#L174-L180
    mask_index = dictionary.add_symbol("[MASK]", is_special=True)
    tokens = list(dictionary.symbols)

    output.parent.mkdir(parents=True, exist_ok=True)
    staged = Path(tempfile.mkdtemp(prefix=f".{output.name}.partial-", dir=output.parent))
    try:
        receipt = {"record_id": declaration["record_id"], "doi": declaration["doi"],
                   "execution": execution,
                   "sources": sources,
                   "dictionary": {"symbols_before_task": symbols_before_mask,
                                  "symbols": tokens,
                                  "task_special_token": {"symbol": "[MASK]", "index": mask_index}},
                   "selection": {"count_per_element": count, "max_atoms": max_atoms,
                                 "max_tokens": max_tokens}, "elements": {}}
        with lmdb.open(str(paths["data"]), subdir=False, readonly=True,
                       lock=False, readahead=False, meminit=False) as environment:
            with environment.begin() as transaction:
                for element in ("H", "C"):
                    with transaction.cursor() as cursor:
                        selected, records = select_records(
                            cursor, element=element, dictionary_tokens=tokens,
                            count=count, max_atoms=max_atoms, max_tokens=max_tokens)
                    files = write_split(staged / element, selected, dictionary_bytes)
                    receipt["elements"][element] = {
                        "records": records, "record_count": len(records),
                        "selected_atom_count": sum(len(record["selected_atom_indices"]) for record in records),
                        "files": files,
                    }
        (staged / "provenance.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
        for root, _, files in os.walk(staged, topdown=False):
            for name in files:
                with (Path(root) / name).open("rb") as stream:
                    os.fsync(stream.fileno())
            artifacts.fsync_directory(Path(root))
        artifacts.publish_directory(staged, output)
        artifacts.fsync_directory(output.parent)
        return receipt
    finally:
        if staged.exists():
            shutil.rmtree(staged)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared-root", type=Path, default=Path(".work/prepared"))
    parser.add_argument("--declaration", type=Path, default=Path("artifacts/release.json"))
    parser.add_argument("--data-selection", type=Path, default=Path("artifacts/data.selection.json"))
    parser.add_argument("--dictionary-selection", type=Path, default=Path("artifacts/weight.selection.json"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--image-id", required=True)
    parser.add_argument("--repository-revision", required=True)
    parser.add_argument("--tool-source", type=Path, action="append", required=True)
    parser.add_argument("--count", type=int, default=2)
    parser.add_argument("--max-atoms", type=int, default=128)
    parser.add_argument("--max-tokens", type=int, default=130)
    args = parser.parse_args()
    prepare(args.prepared_root, args.declaration, args.data_selection, args.dictionary_selection,
            args.output, image_id=args.image_id, repository_revision=args.repository_revision,
            tool_sources=args.tool_source, count=args.count, max_atoms=args.max_atoms, max_tokens=args.max_tokens)


if __name__ == "__main__":
    main()
