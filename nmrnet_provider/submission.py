"""Select exact verified Job bytes for deterministic scientific preparation."""

from tools.container_guard import require_container

require_container()

from nmrnet_provider.input_geometry import InputRejected, prepare_json
from nmrnet_provider.prepared_input import SelectedSmilesSource
from nmrnet_provider.smiles import require_smiles_source
from provider_core.job_input import VerifiedJobInput


def select_job_smiles(verified, *, start_byte, end_byte, requested_count):
    """Bind an explicit interpreter choice without accepting replacement text.

    The interpreter owns what the instructions mean, including N. This checks
    provenance and value bounds only; no text parser independently authorizes N.
    Native graph parsing and worker allocation remain separate operations.
    """
    if type(verified) is not VerifiedJobInput:
        raise TypeError("SMILES selection requires verified original Job instructions.")
    if (
        type(start_byte) is not int
        or type(end_byte) is not int
        or not 0 <= start_byte < end_byte <= len(verified.specification_bytes)
    ):
        raise InputRejected(
            "Cannot select SMILES: start_byte and end_byte must define a nonempty UTF-8 byte span "
            f"within this Job's {len(verified.specification_bytes)} instruction bytes."
        )
    source = verified.specification_bytes[start_byte:end_byte]
    require_smiles_source(source, requested_count)
    return SelectedSmilesSource(
        verified.job_ref, verified.identity.input_fingerprint, start_byte, end_byte, requested_count, source
    )


def prepare_direct_job(verified):
    """Prepare explicit coordinate JSON after the caller admits the Attempt.

    This is the direct-input binding, not an input classifier or interpreter.
    It preserves verified bytes and checks coordinate request structure and units.
    PredictionSession later checks dictionary membership and model input limits.
    """
    if type(verified) is not VerifiedJobInput:
        raise TypeError("Direct Job preparation requires verified original instructions.")
    return prepare_json(verified.specification_bytes)
