"""Load the declared prediction models from read-only extracted release trees.

artifacts/prediction-models.json specifies what may run; extraction receipts
describe what was extracted. Require both to identify the exact bytes handed to
the decoders. The historical 001 packet records their qualification observation,
but its fixtures, predictions and training logs are not runtime dependencies.
See notes/002_label_free_coordinate_prediction_20261014.txt for selection rationale.
"""

from tools.container_guard import require_container

require_container()

import json
import os

from tools import artifacts, baseline_prepare
from . import model


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
        return model.load_prediction_dictionary_from_bytes(content)

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
        return loaded_model, model.load_scaler_from_bytes(scaler)
