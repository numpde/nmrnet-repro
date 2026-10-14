"""Check closed release declarations and exact fixture/producer ownership.

Ports nmrpeak-repro f8055ab1065795fd46f8c1539e894cf9773f5495
repository_checks/nmrpeak_weights.py and checkpoint_release.py (closed identity,
exact member/size/hash), and secs-repro 34f5915c1534278bbf61b4c6592afa44cf94ab08
tests/input/normative/test_fixture_provenance.py (complete corpus inventory,
parent hashes and exact producer bytes). NMRNet receipts retain archive origins;
the checked extraction fixture does not introduce SECS's NMRium/front-end schema.
https://github.com/numpde/nmrpeak-repro/blob/f8055ab1065795fd46f8c1539e894cf9773f5495/repository_checks/nmrpeak_weights.py
https://github.com/numpde/secs-repro/blob/34f5915c1534278bbf61b4c6592afa44cf94ab08/tests/input/normative/test_fixture_provenance.py
"""

from tools.container_guard import require_container

require_container()

import hashlib
import json
import re

from repository_checks.science import source
from repository_checks.science.source import git, live_paths, regular_bytes, relative_path


# Reviewed historical launcher module grants, independently read from
# tools/baseline.sh at dc09d448e3b747a98ec8c5787ec5ac4db308bc2c (run) and
# 6ee0fa1f02dc2838b696d8b17cc48523dd583969 (prepare). The receipt must not
# choose which producer dependencies count as the historical execution.
REFERENCE_PRODUCER_PATHS = {
    "provenance": frozenset(
        {
            "tools/artifacts.py",
            "tools/baseline_prepare.py",
            "tools/baseline_model.py",
            "tools/baseline_output.py",
            "tools/baseline_result.py",
            "tools/baseline_run.py",
        }
    ),
    "preparation_execution": frozenset({"tools/artifacts.py", "tools/baseline_prepare.py"}),
}


def json_document(path):
    def unique_pairs(pairs):
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError("Provenance JSON contains duplicate fields")
        return result

    document = json.loads(regular_bytes(path).decode("utf-8", errors="strict"), object_pairs_hook=unique_pairs)
    if type(document) is not dict:
        raise ValueError("Provenance JSON must be an object")
    return document


def file_identity(path):
    raw = regular_bytes(path)
    return {"size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def require_identity(identity):
    if (
        type(identity) is not dict
        or set(identity) != {"size", "sha256"}
        or type(identity["size"]) is not int
        or identity["size"] <= 0
        or type(identity["sha256"]) is not str
        or re.fullmatch(r"[0-9a-f]{64}", identity["sha256"]) is None
    ):
        raise ValueError("Artifact identity must contain exactly a positive byte length and SHA-256 digest")


def verify_extraction(directory, declaration, selection, *, archive_path):
    receipt = json_document(directory / "provenance.json")
    if set(receipt) != {"archive", "archive_sha256", "members", "producer_sources"}:
        raise ValueError("Extraction receipt must own archive, members and exact producer sources")
    if receipt["archive"] != declaration or re.fullmatch(r"[0-9a-f]{64}", receipt["archive_sha256"]) is None:
        raise ValueError("Extraction receipt archive identity differs from the selected public release")
    archive = regular_bytes(archive_path)
    if len(archive) != declaration["size"] or hashlib.md5(archive, usedforsecurity=False).hexdigest() != declaration["md5"]:
        raise ValueError("Extraction archive bytes differ from the selected public release")
    if hashlib.sha256(archive).hexdigest() != receipt["archive_sha256"]:
        raise ValueError("Extraction receipt archive SHA-256 differs from the actual ZIP bytes")
    names = selection["members"]
    if not names or len(names) != len(set(names)):
        raise ValueError("Selected artifact inventory is empty or duplicated")
    for name in names:
        relative_path(name)
    if type(selection["max_bytes"]) is not int or selection["max_bytes"] <= 0:
        raise ValueError("Selected artifact byte budget must be a positive integer")
    members = receipt["members"]
    actual = set(live_paths(directory, (path.name for path in directory.iterdir()))) - {"provenance.json"}
    if set(members) != set(names) or set(members) != actual:
        raise ValueError("Every extracted corpus file must have exactly one selected provenance record")
    for name, identity in members.items():
        require_identity(identity)
        if file_identity(directory / name) != identity:
            raise ValueError(f"Extracted corpus bytes differ from provenance: {name}")
    if sum(identity["size"] for identity in members.values()) > selection["max_bytes"]:
        raise ValueError("Extracted corpus exceeds its selection byte budget")
    producers = receipt["producer_sources"]
    if set(producers) != {"tools/artifacts.py"}:
        raise ValueError("Extraction receipt does not own the complete exact producer input set")
    return producers


def verify_producers(root, producers, revision=None):
    if not producers:
        raise ValueError("Producer source inventory is empty")
    for name, identity in producers.items():
        relative_path(name)
        require_identity(identity)
        raw = regular_bytes(root / name) if revision is None else git(root, "cat-file", "blob", f"{revision}:{name}")
        if {"size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()} != identity:
            raise ValueError(f"Producer bytes differ from their recorded identity: {name}")


def verify_reference_execution(root, execution, phase):
    """Bind the complete historical producer inventory before checking bytes."""
    if set(execution["tool_sources"]) != REFERENCE_PRODUCER_PATHS[phase]:
        raise ValueError(f"Reference {phase} does not own its complete exact historical producer input set")
    if re.fullmatch(r"sha256:[0-9a-f]{64}", execution["image_id"]) is None:
        raise ValueError("Reference execution image is not immutable")
    revision = execution["repository_revision"]
    if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise ValueError("Reference execution revision is not one full Git object ID")
    verify_producers(root, execution["tool_sources"], revision)


def verify_release(root):
    from tools.artifacts import load_declaration

    document = json_document(root / "artifacts/release.json")
    if set(document) != {"record_id", "doi", "version_index", "publication_date", "metadata_url", "files"}:
        raise ValueError("Public release declaration fields are not exact")
    if type(document["version_index"]) is not int or document["version_index"] < 0:
        raise ValueError("Public release version index must be a nonnegative integer")
    if document["metadata_url"] != f"https://zenodo.org/api/records/{document['record_id']}":
        raise ValueError("Public release metadata URL differs from its record")
    if type(document["publication_date"]) is not str or re.fullmatch(r"\d{4}-\d{2}-\d{2}", document["publication_date"]) is None:
        raise ValueError("Public release date must identify the recorded publication date")
    if type(document["files"]) is not list or not document["files"]:
        raise ValueError("Public release archive inventory is empty or invalid")
    for archive in document["files"]:
        if type(archive) is not dict or set(archive) != {"name", "url", "size", "md5"}:
            raise ValueError("Public archive declaration fields are not exact")
    load_declaration(root / "artifacts/release.json")
    archives = {item["name"]: item for item in document["files"]}
    selections = sorted((root / "artifacts").glob("*.selection.json"))
    if {path.stem.removesuffix(".selection") + ".zip" for path in selections} != set(archives):
        raise ValueError("Every public archive must have exactly one owned selection declaration")
    for path in selections:
        selection = json_document(path)
        if set(selection) != {"members", "max_bytes"} or not selection["members"]:
            raise ValueError("Archive selection fields must be exact and nonempty")
        names = selection["members"]
        if len(names) != len(set(names)):
            raise ValueError("Archive selection repeats a member")
        for name in names:
            relative_path(name)
        if type(selection["max_bytes"]) is not int or selection["max_bytes"] <= 0:
            raise ValueError("Archive selection byte budget must be a positive integer")
    return archives


def verify_reference(root):
    """Authenticate the retained historical reference against its original inputs.

    Historical producer records are compared with their recorded Git revision,
    not today's edited producer. This keeps the snapshot's authority explicit.
    """
    reference = json_document(root / "artifacts/reference-cpu-float32.json")
    archives = verify_release(root)
    for phase in ("provenance", "preparation_execution"):
        verify_reference_execution(root, reference[phase], phase)
    if set(reference["source_artifacts"]) != {"data", "dictionary"} or set(reference["elements"]) != {"H", "C"}:
        raise ValueError("Reference must own exactly its two input sources and both qualified elements")
    for source in reference["source_artifacts"].values():
        if source["archive"] != archives[source["archive"]["name"]]:
            raise ValueError("Reference source archive differs from the declared public release")
        relative_path(source["member"])
        require_identity({key: source[key] for key in ("size", "sha256")})
        if re.fullmatch(r"[0-9a-f]{64}", source["archive_sha256"]) is None:
            raise ValueError("Reference source archive has no exact authenticated byte identity")
    for element, facts in reference["elements"].items():
        if facts["fold"] != 0 or set(facts["artifacts"]) != {"checkpoint", "scaler", "infer_log", "finetune_log"}:
            raise ValueError(f"Reference {element} model does not own one complete fold-0 checkpoint configuration")
        archive = dict(facts["checkpoint_archive"])
        digest = archive.pop("sha256")
        if archive != archives["checkpoints.zip"] or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValueError("Reference checkpoint archive differs from its public release")
        for item in facts["artifacts"].values():
            relative_path(item["member"])
            require_identity({key: item[key] for key in ("size", "sha256")})


def verify_reference_source_qualification(root):
    """Bind retrospectively inspected image bytes without rewriting execution facts.

    The retained reference predates source manifests. Its supplement records a
    separate, offline inspection of that exact immutable image, including every
    LICENSE/uninmr file. Local capture hashes preserve the audit trail; ordinary
    checks consume the reviewed public observation without requiring .work.
    """
    reference_path = root / "artifacts/reference-cpu-float32.json"
    reference = json_document(reference_path)
    qualification = json_document(root / "artifacts/reference-source-qualification.json")
    fields = {
        "qualification",
        "reference_sha256",
        "source_paths_sha256",
        "source_closure_sha256",
        "image_observation",
        "evidence",
    }
    if set(qualification) != fields or qualification["qualification"] != "retrospective_image_source_closure":
        raise ValueError("Cannot qualify reference source: the supplement must identify a retrospective image source audit")
    for field, path in (
        ("reference_sha256", reference_path),
        ("source_paths_sha256", root / source.DECLARATION),
        ("source_closure_sha256", root / source.MANIFEST),
    ):
        if qualification[field] != hashlib.sha256(regular_bytes(path)).hexdigest():
            raise ValueError(f"Cannot qualify reference source: {field} does not bind the retained {path.name} bytes")
    revision, _ = source.read_declaration(root / source.DECLARATION)
    observation = qualification["image_observation"]
    if type(observation) is not dict or set(observation) != {"id", "upstream_revision", "files"}:
        raise ValueError(
            "Cannot qualify reference source: the image observation must own its image, revision and complete file hashes"
        )
    if observation["id"] != reference["provenance"]["image_id"]:
        raise ValueError("Cannot qualify reference source: the inspected image differs from the historical execution image")
    if observation["upstream_revision"] != revision or revision != reference["provenance"]["upstream_revision"]:
        raise ValueError(
            "Cannot qualify reference source: the inspected image revision differs from the declared historical source"
        )
    if observation["files"] != source.read_manifest(root / source.MANIFEST):
        raise ValueError("Cannot qualify reference source: the inspected image files differ from the complete source manifest")
    evidence = qualification["evidence"]
    names = {
        "qualify.py",
        "qualify.sh",
        "qualify.command",
        "qualify.json",
        "qualify.stderr",
        "qualify.exit",
        "image-inspect.json",
        "qualified-source.py",
        "qualified-container_guard.py",
    }
    if (
        type(evidence) is not dict
        or set(evidence) != {"directory", "files"}
        or evidence["directory"] != ".work/qualification/source-closure-20261014"
        or type(evidence["files"]) is not dict
        or set(evidence["files"]) != names
    ):
        raise ValueError("Cannot qualify reference source: the audit trail must identify every retained qualification capture")
    for name, identity in evidence["files"].items():
        if name in {"qualify.stderr", "qualify.exit"}:
            raw = b"" if name == "qualify.stderr" else b"0\n"
            if identity != {"size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}:
                raise ValueError(f"Cannot qualify reference source: {name} does not record a successful audit")
        else:
            require_identity(identity)
    source.verify_parent_gitlink(root)
    for phase in ("provenance", "preparation_execution"):
        historical = reference[phase]["repository_revision"]
        expected = f"160000 commit {revision}\t{source.UPSTREAM}\0".encode("ascii")
        if git(root, "ls-tree", "-z", historical, "--", source.UPSTREAM.as_posix()) != expected:
            raise ValueError(
                f"Cannot qualify reference source: the historical {phase} gitlink differs from the inspected image source"
            )
    verify_reference(root)


def reference_attribution_inventory(reference):
    """Borrow artifact identities and parents from the retained reference owner.

    Execution pointers identify local preparation/qualification consumers. They
    do not claim those consumers originally generated the public model files.
    The independent attribution author must identify the original generator and
    its evidence; original model training producer proof is not in this record.
    """
    inventory = {}
    for role, facts in reference["source_artifacts"].items():
        inventory[f"source_artifacts/{role}"] = {
            "identity": {key: facts[key] for key in ("size", "sha256")},
            "parents": [{"archive": facts["archive"]["name"], "sha256": facts["archive_sha256"]}],
            "consumer_execution": "preparation_execution",
        }
    for element, facts in reference["elements"].items():
        for role, member in facts["artifacts"].items():
            inventory[f"elements/{element}/artifacts/{role}"] = {
                "identity": {key: member[key] for key in ("size", "sha256")},
                "parents": [{"archive": facts["checkpoint_archive"]["name"], "sha256": facts["checkpoint_archive"]["sha256"]}],
                "consumer_execution": "provenance",
            }
    return inventory


def verify_attribution_document(document, reference, reference_sha256):
    """Preserve SECS per-file descriptions, origins, bytes and parent bindings.

    Source: secs-repro 34f5915c1534278bbf61b4c6592afa44cf94ab08,
    tests/input/normative/test_fixture_provenance.py: assert_origin,
    test_every_corpus_file_has_one_record and test_bytes_and_attribution_match_the_record.
    https://github.com/numpde/secs-repro/blob/34f5915c1534278bbf61b4c6592afa44cf94ab08/tests/input/normative/test_fixture_provenance.py
    """
    if type(document) is not dict or set(document) != {"reference_sha256", "files"}:
        raise ValueError("Source attribution must contain exactly reference_sha256 and files")
    if document["reference_sha256"] != reference_sha256:
        raise ValueError("Source attribution does not identify the exact retained reference bytes")
    records = document["files"]
    if type(records) is not list or not records:
        raise ValueError("Source attribution file inventory must be a nonempty list")
    expected = reference_attribution_inventory(reference)
    observed = []
    for record in records:
        if type(record) is not dict or set(record) != {"artifact_ref", "identity", "description", "origin"}:
            raise ValueError("Source attribution record fields are not exact")
        name = record["artifact_ref"]
        if type(name) is not str or name not in expected:
            raise ValueError("Source attribution names an unknown retained source artifact")
        observed.append(name)
        if type(record["description"]) is not str or not record["description"].strip():
            raise ValueError("Source attribution record must have a nonempty description")
        require_identity(record["identity"])
        if record["identity"] != expected[name]["identity"]:
            raise ValueError("Source attribution artifact bytes differ from the retained reference identity")
        origin = record["origin"]
        origin_fields = {
            "author",
            "generator",
            "generator_evidence",
            "licence",
            "licence_text",
            "basis",
            "parents",
            "consumer_execution",
        }
        if type(origin) is not dict or set(origin) != origin_fields:
            raise ValueError("Source attribution origin fields are not exact")
        author = origin["author"]
        if not (author is None or type(author) is str and author.strip()):
            raise ValueError("Source attribution author must be null or nonempty text")
        for field in ("generator", "generator_evidence", "licence", "licence_text", "basis"):
            if type(origin[field]) is not str or not origin[field].strip():
                raise ValueError(f"Source attribution {field} must be nonempty text")
        if origin["parents"] != expected[name]["parents"]:
            raise ValueError("Source attribution archive parent identities differ from the retained reference")
        if origin["consumer_execution"] != expected[name]["consumer_execution"]:
            raise ValueError("Source attribution names a different reference consumer execution")
    if len(observed) != len(set(observed)) or set(observed) != set(expected):
        raise ValueError("Every retained model and input source artifact must have exactly one attribution record")


def verify_attribution(root):
    """Require independently authored attribution without manufacturing facts.

    Missing source-attribution.json is a policy failure. Existing reference
    verification owns release/parent identities and historical consumer producer
    bytes; attribution adds human provenance rather than a second source oracle.
    Generator evidence is an authored reference, not proof of external training
    code identity, which the retained reference does not supply.

    Declaration schema: reference_sha256 binds the existing retained JSON bytes;
    files contains exactly the two source_artifacts and eight H/C artifact_refs.
    Each record owns description and origin text, and borrows size/SHA256 in its
    identity. Origin owns author (null or named), generator, generator_evidence,
    licence, licence_text and basis. Its parents must equal the existing public
    archive name/SHA256 and consumer_execution must point to preparation_execution
    for inputs or provenance for model qualification. Existing verify_reference
    authenticates those consumer source inventories against historical Git.
    No second producer declaration, licence guess or SECS dataset licence is
    supplied. Exact original model training producer proof has no authority in
    this reference and remains an explicit capability limit.
    """
    document = json_document(root / "artifacts/source-attribution.json")
    raw_reference = regular_bytes(root / "artifacts/reference-cpu-float32.json")
    reference = json_document(root / "artifacts/reference-cpu-float32.json")
    verify_attribution_document(document, reference, hashlib.sha256(raw_reference).hexdigest())
    verify_reference(root)
