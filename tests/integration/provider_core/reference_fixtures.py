"""Private input export and independent population checks for connected science.

The original JSON encoding is from reference_requests at 3bd6114,
tests/test_prediction_release.py. XYZ preserves those values, while the native
reader deliberately emits its own compact JSON encoding. Never equate those two
byte hashes or derive expected predictions from the newly executed worker.
"""

import hashlib
import json
import math
from collections import Counter
from fractions import Fraction
from pathlib import Path

from nmrnet_provider.xyz import prepare_xyz

REFERENCE_SHA256 = "8268206ad12003dea5ef57e1a7392de69325e110b54f2b91ee0f6314c4fef5ae"
PRODUCTION_SHA256 = "3054ace5fb17b824e114c65c25e24389eca7551e38a0aece7f347f5b98f5fa75"


def pinned_packet(path, digest):
    with Path(path).open("rb") as stream:
        content = stream.read(1024 * 1024 + 1)
    if len(content) > 1024 * 1024 or hashlib.sha256(content).hexdigest() != digest:
        raise AssertionError("Scientific oracle bytes differ from the pinned packet")
    return json.loads(content)


def original_request_bytes(request):
    return json.dumps(
        {
            "coordinate_unit": request["coordinate_unit"],
            "atoms": request["atoms"],
            "coordinates": request["coordinates"],
        },
        allow_nan=False,
    ).encode()


def render_fixture(requests, origins, *, case):
    """Export source inputs only; retain original producers outside model requests."""
    lines, records = [], []
    for ordinal, (element, key, request) in enumerate(requests, 1):
        raw = original_request_bytes(request)
        first = len(lines) + 3
        lines.extend((str(len(request["atoms"])), "Private qualification geometry " + str(ordinal)))
        lines.extend(
            atom + " " + " ".join(repr(float(value)) for value in row)
            for atom, row in zip(request["atoms"], request["coordinates"], strict=True)
        )
        records.append(
            {
                "source_element": element,
                "source_key_hex": key,
                "raw_sha256": origins[(element, key)],
                "source_record_number": ordinal,
                "source_positions": list(range(first, first + len(request["atoms"]))),
                "original_request": request,
                "original_request_identity": {"size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()},
            }
        )
    source = ("\n".join(lines) + "\n").encode("ascii")
    manifest = {"case": case, "source": {"size": len(source), "sha256": hashlib.sha256(source).hexdigest()}, "records": records}
    verify_fixture(source, manifest)
    return source, manifest


def verify_fixture(source, manifest):
    """Check exact round-trip input values and source positions before use."""
    if manifest["source"] != {"size": len(source), "sha256": hashlib.sha256(source).hexdigest()}:
        raise AssertionError("Exported XYZ bytes differ from the source manifest")
    prepared = prepare_xyz(source, source_unit="angstrom", selection="all")
    for geometry, record in zip(prepared, manifest["records"], strict=True):
        raw = original_request_bytes(record["original_request"])
        if record["original_request_identity"] != {"size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}:
            raise AssertionError("Exported original request differs from its identity")
        if original_request_bytes(json.loads(geometry.request_bytes)) != raw:
            raise AssertionError("XYZ conversion changed original atom order or coordinates")
        if (geometry.source_record_number, list(geometry.source_positions)) != (
            record["source_record_number"],
            record["source_positions"],
        ):
            raise AssertionError("XYZ conversion changed original record or line correspondence")
    return prepared


def indexed_predictions(identities, predictions):
    """Require unique producer/nucleus/atom keys before exact population comparison."""
    observed, producers = {}, set()
    for producer, rows in zip(identities, predictions, strict=True):
        if producer in producers:
            raise AssertionError("Scientific population contains a duplicate producer")
        producers.add(producer)
        for row in rows:
            key = (*producer, row["element"], row["atom_index"])
            if key in observed:
                raise AssertionError("Scientific population contains a duplicate prediction")
            if not math.isfinite(row["shift_ppm"]):
                raise AssertionError("Scientific population contains a nonfinite shift")
            observed[key] = row["shift_ppm"]
    return observed


def require_reference_inputs(manifest, reference, production):
    """Bind exported inputs to original producers and the previously qualified requests."""
    expected = production["qualification"]["real_model_callable"]["fixture_observations"]
    for record, fixture in zip(manifest["records"], expected, strict=True):
        producer = (fixture["source_element"], fixture["source_key_hex"])
        original = next(item for item in reference["elements"][producer[0]]["records"] if item["key_hex"] == producer[1])
        if (record["source_element"], record["source_key_hex"]) != producer:
            raise AssertionError("Exported producer population/order differs from the original reference")
        if record["raw_sha256"] != original["raw_sha256"] or record["raw_sha256"] != fixture["source_record_sha256"]:
            raise AssertionError("Exported source record differs from the original producer bytes")
        if record["original_request_identity"] != fixture["request"]:
            raise AssertionError("Exported coordinates differ from the previously qualified request bytes")


def _require_exact_population(observed, expected, subject):
    """Name a bounded first mismatch rather than dump the molecular result."""
    missing, unexpected = expected.keys() - observed.keys(), observed.keys() - expected.keys()
    if missing or unexpected:
        raise AssertionError(
            f"{subject} keys differ: {len(missing)} missing, {len(unexpected)} unexpected; "
            f"first five missing {sorted(missing)[:5]}, unexpected {sorted(unexpected)[:5]}"
        )
    for key, value in expected.items():
        if observed[key] != value:
            raise AssertionError(f"{subject} differs at {key}: observed {observed[key]!r}, original {value!r}")


def require_reference_predictions(records, predictions, reference, production):
    """Compare complete producer/nucleus/atom populations, then the original mask."""
    qualified = production["qualification"]["real_model_callable"]
    fixtures = qualified["fixture_observations"]
    expected = indexed_predictions(
        [(item["source_element"], item["source_key_hex"]) for item in fixtures],
        [item["result"]["predictions"] for item in fixtures],
    )
    if len(expected) != 137 or Counter(key[2] for key in expected) != {"H": 82, "C": 55}:
        raise AssertionError("Original production oracle does not contain the qualified 137-output population")
    observed = indexed_predictions([(item["source_element"], item["source_key_hex"]) for item in records], predictions)
    _require_exact_population(observed, expected, "Complete connected population")
    historical = {}
    for element in ("H", "C"):
        for item in reference["elements"][element]["predictions"]:
            key = (element, item["key_hex"], element, item["atom_index"])
            if key in historical:
                raise AssertionError("Original historical population repeats a producer/nucleus/atom identity")
            historical[key] = item["prediction_ppm"]
    original_mask = {
        (item["element"], item["key_hex"], item["element"], item["atom_index"])
        for item in qualified["exact_reference_population"]
    }
    if set(historical) != original_mask or Counter(key[2] for key in historical) != {"H": 42, "C": 22}:
        raise AssertionError("Historical 64-position mask differs from the independently recorded original population")
    _require_exact_population({key: observed[key] for key in historical}, historical, "Historical 64-position population")
    return {
        "complete_predictions": len(observed),
        "by_nucleus": dict(Counter(key[2] for key in observed)),
        "historical_positions": len(historical),
    }


def require_capacity_inputs(manifest):
    records = manifest["records"]
    if [(item["source_element"], item["source_key_hex"]) for item in records] != [("H", "313238"), ("C", "313238")]:
        raise AssertionError("Capacity input must contain exactly the ordered H128 and C128 producers")
    for element, record in zip(("H", "C"), records, strict=True):
        expected = {
            "coordinate_unit": "angstrom",
            "atoms": [element] * 128,
            "coordinates": [[index * 0.1, 0.0, 0.0] for index in range(128)],
        }
        if record["original_request"] != expected:
            raise AssertionError("Capacity input differs from the fixed independent 128-atom coordinate recipe")


# Original authored chloromethane, H/Cl/C/H/H, expressed in nm. Expected angstrom
# numbers are the independent literals in expected.json, not reader output.
NM_TABLE = b"H 0.1027 0 -0.0363\nCl 0 0 0.178\nC 0 0 0\nH -0.0514 0.089 -0.0363\nH -0.0514 -0.089 -0.0363\n"
PREFIX = "é: supplied coordinates in nm\n".encode()


def nm_conversion_oracle(source_nm, intended_angstrom):
    """Bound two binary64 roundings using fixed, independent decimal literals.

    Source parsing contributes 10 times its exact representational error.
    Multiplication by the exactly representable 10 contributes at most half
    an ULP of the rounded exact binary product. Fraction arithmetic keeps this
    budget and the intended angstrom value exact; no worker output sets it.
    The unit's exact factor is documented in input_geometry.coordinate_conversion.
    """
    source, intended = Fraction(source_nm), Fraction(intended_angstrom)
    if source * 10 != intended:
        raise AssertionError("Fixed nm and angstrom fixture literals disagree about the exact times-ten conversion")
    if source == 0:
        return intended, Fraction(0)
    parsed = Fraction.from_float(float(source))
    parse_error = abs(parsed - source) * 10
    product_rounding = Fraction.from_float(math.ulp(float(parsed * 10))) / 2
    return intended, parse_error + product_rounding
