"""Authored numerical populations exercise the gate; these are not real shifts.

Counts mirror the qualified population. Positions and values are synthetic, with
one H position deliberately outside the original mask in the first H producer.
The native fixture separately uses the pinned original packets without rewriting.
"""


def authored_populations():
    records, predictions, fixtures, mask = [], [], [], []
    reference = {"elements": {element: {"predictions": []} for element in ("H", "C")}}
    for dataset, key, hydrogens, carbons, historical_count in (
        ("H", "35333738", 26, 15, 25),
        ("H", "35333739", 17, 18, 17),
        ("C", "30", 24, 15, 15),
        ("C", "31", 15, 7, 7),
    ):
        rows = [
            {"element": element, "atom_index": index, "shift_ppm": float(index) + 0.25}
            for element, count, offset in (("C", carbons, 0), ("H", hydrogens, carbons))
            for index in range(offset, offset + count)
        ]
        records.append({"source_element": dataset, "source_key_hex": key})
        predictions.append([dict(row) for row in rows])
        fixtures.append({"source_element": dataset, "source_key_hex": key, "result": {"predictions": rows}})
        for row in [item for item in rows if item["element"] == dataset][:historical_count]:
            reference["elements"][dataset]["predictions"].append(
                {"key_hex": key, "atom_index": row["atom_index"], "prediction_ppm": row["shift_ppm"]}
            )
            mask.append({"element": dataset, "key_hex": key, "atom_index": row["atom_index"]})
    production = {
        "qualification": {"real_model_callable": {"fixture_observations": fixtures, "exact_reference_population": mask}}
    }
    return records, predictions, reference, production
