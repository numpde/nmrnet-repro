"""Connect verified Job instructions to existing direct-coordinate preparation."""

from tools.container_guard import require_container

require_container()

from nmrnet_provider.input_geometry import prepare_json
from provider_core.job_input import VerifiedJobInput


def prepare_direct_job(verified):
    """Prepare explicit coordinate JSON after the caller admits the Attempt.

    This is the direct-input binding, not an input classifier or interpreter.
    It preserves verified bytes and checks coordinate request structure and units.
    PredictionSession later checks dictionary membership and model input limits.
    """
    if type(verified) is not VerifiedJobInput:
        raise TypeError("Direct Job preparation requires verified original instructions.")
    return prepare_json(verified.specification_bytes)
