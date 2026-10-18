"""Load the declared prediction models from read-only extracted release trees.

artifacts/prediction-models.json specifies what may run; extraction receipts
describe what was extracted. Require both to identify the exact bytes handed to
the decoders. The historical 001 packet records their qualification observation,
but its fixtures, predictions and training logs are not runtime dependencies.
See notes/002_label_free_coordinate_prediction_20261014.txt for selection rationale.
"""

from tools.container_guard import require_container

require_container()

import copy
import importlib.metadata
import json
import os
import platform
import warnings

from tools import artifacts, baseline_prepare

from . import model, predict


class SelectedReleaseModels:
    """Supply predict_request's loaders without retaining models between heads.

    Construction captures paths only. The first admitted dictionary load reads
    the trusted, read-only model declaration once; failed request/unit admission
    therefore performs no artifact I/O. Mount both extracted roots read-only.
    """

    def __init__(self, *, declaration_path, weight_root, checkpoint_root):
        self.declaration_path = declaration_path
        self.weight_root = weight_root
        self.checkpoint_root = checkpoint_root
        self._specification = None
        self._observed = {"dictionary": None, "elements": {}}
        self._warnings = []

    def observed_artifacts(self):
        """Snapshot the decoded dictionary and successfully loaded model/scaler pairs.

        Observations accumulate over this loader's lifetime. Verification precedes
        decoding; a selected but unloaded or failed pair is absent.
        The released worker creates one loader per session, so its final snapshot
        cannot accidentally include another execution's observations.
        """
        return copy.deepcopy(self._observed)

    def observed_warnings(self):
        """Return private warning evidence, not text approved for publication."""
        return copy.deepcopy(self._warnings)

    def _selection(self):
        if self._specification is None:
            # This five-member declaration is small; cap actual consumption even
            # if the mounted file grows while being read.
            with artifacts.regular_file(self.declaration_path, os.O_RDONLY) as stream:
                content = stream.read(64 * 1024 + 1)
            if len(content) > 64 * 1024:
                raise ValueError("Cannot load prediction models: declaration exceeds the 64 KiB byte limit.")
            self._specification = json.loads(content)
        return self._specification

    def _verified_bytes(self, root, source, expected, role):
        content, identity = baseline_prepare.verified_member_bytes(
            root, source["archive"],
            {"members": [expected["member"]], "max_bytes": expected["size"]},
        )
        for field in ("member", "size", "sha256"):
            if identity[field] != expected[field]:
                raise ValueError(f"Cannot load {role}: {field} differs from the declared prediction model.")
        if identity["archive_sha256"] != source["archive_sha256"]:
            raise ValueError(f"Cannot load {role}: archive SHA-256 differs from the declared prediction model.")
        return content

    def load_dictionary(self):
        specification = self._selection()
        expected = specification["dictionary"]
        content = self._verified_bytes(self.weight_root, expected, expected, "prediction dictionary")
        dictionary = model.load_prediction_dictionary_from_bytes(content)
        self._observed["dictionary"] = _artifact_identity(expected, expected)
        return dictionary

    def load_selected_element(self, element, dictionary):
        """Verify both paired members before either trusted object is decoded."""
        if element not in ("H", "C"):
            raise ValueError("Cannot load prediction model: element must be H or C.")
        specification = self._selection()
        selected = specification["elements"][element]
        source = specification["checkpoint_archive"]
        checkpoint = self._verified_bytes(
            self.checkpoint_root, source, selected["checkpoint"], f"{element} checkpoint"
        )
        scaler = self._verified_bytes(
            self.checkpoint_root, source, selected["scaler"], f"{element} scaler"
        )
        loaded_model = model.load_prediction_model_from_bytes(checkpoint, dictionary)
        del checkpoint
        loaded_scaler = self._load_scaler(scaler)
        self._observed["elements"][element] = {
            role: _artifact_identity(source, selected[role]) for role in ("checkpoint", "scaler")
        }
        return loaded_model, loaded_scaler

    def _load_scaler(self, content):
        # One serial worker owns this warning hook. Retain the existing filter
        # policy (including warnings-as-errors) and forward each actual emission.
        # Avoid catch_warnings here: changing filter versions would invalidate
        # default-policy suppression of warnings already emitted by this process.
        previous = warnings.showwarning

        # silent-defaults: allow-contract -- Match Python's warning-hook signature; forwarding preserves the destination and optional source line.
        def observe(message, category, filename, lineno, file=None, line=None):
            self._warnings.append({
                "category": category.__module__ + "." + category.__qualname__,
                "message": str(message), "filename": filename, "lineno": lineno,
            })
            previous(message, category, filename, lineno, file=file, line=line)

        warnings.showwarning = observe
        try:
            return model.load_scaler_from_bytes(content)
        finally:
            warnings.showwarning = previous


def _artifact_identity(source, member):
    """Project the identities already compared with decoded bytes, without URLs."""
    return {"archive_sha256": source["archive_sha256"], **{name: member[name] for name in ("member", "size", "sha256")}}


class ReleasedPredictionSession:
    """Attach worker-owned observations without changing scientific calculation.

    This is a private worker result, not the published scientific envelope.
    Distribution versions describe installed metadata, not imported-code identity;
    image/source admission remains the host composition's responsibility.
    """

    def __init__(self, models):
        self._models = models
        self._prediction = predict.PredictionSession(models.load_dictionary, models.load_selected_element)

    def prepare(self, raw):
        handle = self._prediction.prepare(raw)
        # Obtain required runtime metadata before expensive model execution; a
        # missing distribution must not lose an already computed result.
        self._runtime = {
            "python": platform.python_version(),
            "installed_distributions": {
                name: importlib.metadata.version(name) for name in ("torch", "numpy", "scikit-learn", "joblib")
            },
        }
        return handle

    def execute(self, handle):
        prediction = self._prediction.execute(handle)
        return {
            "prediction": prediction,
            "provenance": {"artifacts": self._models.observed_artifacts(), "runtime": self._runtime},
            "private_warnings": self._models.observed_warnings(),
        }
