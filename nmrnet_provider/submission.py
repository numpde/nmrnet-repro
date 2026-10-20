"""Associate bounded interpreter-supplied sources with their verified Job."""

from tools.container_guard import require_container

require_container()

from nmrnet_provider.geometry_protocol import require_read_options
from nmrnet_provider.input_geometry import InputRejected, decode_source, prepare_json
from nmrnet_provider.prepared_input import SelectedPastedSource, SelectedSmilesSource
from nmrnet_provider.smiles import require_smiles_source
from provider_core.job_input import VerifiedJobInput


def select_job_smiles(verified, *, smiles, requested_count):
    """Admit received SMILES and count without claiming exact Job extraction.

    The interpreter owns what the instructions mean, including N. This checks
    Job association and value bounds only; no prose parser authorizes N.
    Native graph parsing and worker allocation remain separate operations.
    """
    source = _received_source(verified, smiles, subject="SMILES")
    require_smiles_source(source, requested_count)
    return SelectedSmilesSource(verified.job_ref, verified.identity.input_fingerprint, requested_count, source)


def _received_source(verified, text, *, subject):
    """Freeze tool text; deterministic readers own syntax and scientific checks."""
    if type(verified) is not VerifiedJobInput:
        raise TypeError(f"{subject} selection requires verified original Job instructions.")
    if type(text) is not str:
        raise InputRejected(f"Cannot read {subject}: supply text.")
    try:
        source = text.encode("utf-8")
    except UnicodeEncodeError:
        raise InputRejected(f"Cannot read {subject}: supply valid UTF-8 text.") from None
    decode_source(source)
    return source


def select_job_pasted(verified, *, source_text, source_format, source_unit):
    """Bind exactly one declared coordinate source without guessing format or units."""
    source = _received_source(verified, source_text, subject="pasted coordinates")
    if source_format not in ("table", "xyz"):
        raise InputRejected("Cannot read pasted coordinates: choose table or xyz; use Uploads for other structure formats.")
    selection = None
    if source_format == "xyz":
        selection = "all"
    require_read_options(source_format, source_unit, selection)
    return SelectedPastedSource(verified.job_ref, verified.identity.input_fingerprint, source_format, source_unit, source)


def prepare_direct_job(verified):
    """Prepare explicit coordinate JSON after the caller admits the Attempt.

    This is the direct-input binding, not an input classifier or interpreter.
    It preserves verified bytes and checks coordinate request structure and units.
    PredictionSession later checks dictionary membership and model input limits.
    """
    if type(verified) is not VerifiedJobInput:
        raise TypeError("Direct Job preparation requires verified original instructions.")
    return prepare_json(verified.specification_bytes)
