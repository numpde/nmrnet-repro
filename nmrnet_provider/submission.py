"""Carry an input owner's frozen geometry collection into scientific admission."""

from tools.container_guard import require_container

require_container()

from dataclasses import dataclass, field

from nmrnet_provider.input_geometry import UploadSource, prepare_json
from provider_core.job_input import VerifiedJobInput


@dataclass(frozen=True, slots=True)
class PreparedJobInput:
    """An input owner's ordered values; ResultPlan owns their final admission."""

    geometries: tuple = field(repr=False)
    preparation_method: str
    source_upload: UploadSource | None = field(default=None, repr=False)


def prepare_direct_job(verified):
    """Prepare explicit coordinate JSON after the caller admits the Attempt.

    This is the direct-input binding, not an input classifier or interpreter.
    It preserves verified bytes and checks coordinate request structure and units.
    PredictionSession later checks dictionary membership and model input limits.
    """
    if type(verified) is not VerifiedJobInput:
        raise TypeError("Direct Job preparation requires verified original instructions.")
    return prepare_json(verified.specification_bytes)
