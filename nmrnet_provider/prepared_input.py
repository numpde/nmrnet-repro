"""Freeze source selections, scientific inputs and output settings without API I/O.

Optional interpretation bytes are the trusted chooser's secret-free observation
projection. They describe its work; they are not model arguments or authority.
"""

from tools.container_guard import require_container

require_container()

import hashlib
from dataclasses import dataclass, field

from nmrnet_provider.input_geometry import UploadMemberSource, UploadSource
from nmrnet_provider.smiles import GeneratedConformers
from provider_core.result_verbosity import ResultVerbosity

DEFAULT_SHIFT_SIGNIFICANT_DIGITS = 4


def require_shift_significant_digits(value):
    if type(value) is not int or not 1 <= value <= 17:
        raise ValueError("The shift_significant_digits field must be an integer from 1 to 17.")
    return value


@dataclass(frozen=True, slots=True)
class OutputSettings:
    """One presentation choice shared by direct input and interpreter tools.

    Precision is NMRNet policy. The shared enum supplies vocabulary only; it
    neither chooses the default nor knows any scientific result fields.
    """

    verbosity: ResultVerbosity = ResultVerbosity.MEDIUM
    shift_significant_digits: int = DEFAULT_SHIFT_SIGNIFICANT_DIGITS

    def __post_init__(self):
        if type(self.verbosity) is not ResultVerbosity:
            raise ValueError("The verbosity field must be an admitted low, medium or max choice.")
        require_shift_significant_digits(self.shift_significant_digits)

    @classmethod
    def from_json(cls, value):
        if type(value) is not dict or not set(value) <= {"verbosity", "shift_significant_digits"}:
            raise ValueError("The output field must be an object containing only verbosity and shift_significant_digits.")
        return cls(
            verbosity=ResultVerbosity.parse(value.get("verbosity", DEFAULT_OUTPUT.verbosity.value)),
            shift_significant_digits=value.get("shift_significant_digits", DEFAULT_OUTPUT.shift_significant_digits),
        )

    def as_dict(self):
        return {"verbosity": self.verbosity.value, "shift_significant_digits": self.shift_significant_digits}


DEFAULT_OUTPUT = OutputSettings()


@dataclass(frozen=True, slots=True)
class SelectedSmilesSource:
    """Immutable interpreter-supplied SMILES associated with a verified Job.

    This records received text, not an exact extraction or proof of prose intent.
    """

    job_ref: str
    input_fingerprint: str
    requested_count: int
    source_bytes: bytes = field(repr=False)

    @property
    def source_sha256(self):
        return hashlib.sha256(self.source_bytes).hexdigest()


@dataclass(frozen=True, slots=True)
class GeneratedJobInput:
    """Join the Job selection to native output; ResultPlan admits their agreement.

    The input owner associates interpreter-supplied SMILES with the Job and
    confirms generator retirement before handoff. The pair grants no authority.
    """

    source: SelectedSmilesSource
    generated: GeneratedConformers = field(repr=False)
    interpretation: bytes | None = field(default=None, repr=False)
    output: OutputSettings = field(default=DEFAULT_OUTPUT, kw_only=True)


@dataclass(frozen=True, slots=True)
class SelectedPastedSource:
    """Immutable interpreter-supplied coordinates associated with a verified Job.

    Reader line numbers are relative to the received text. Format and unit are
    chooser decisions; the reader owns parsing and conversion to angstroms.
    """

    job_ref: str
    input_fingerprint: str
    source_format: str
    source_unit: str
    source_bytes: bytes = field(repr=False)

    @property
    def source_sha256(self):
        return hashlib.sha256(self.source_bytes).hexdigest()


@dataclass(frozen=True, slots=True)
class PreparedJobInput:
    """Supplied coordinates; ResultPlan owns their final scientific admission.

    GeneratedJobInput carries the distinct Job selection and native atom origins
    instead of inventing file records or supplied-coordinate metadata.
    """

    geometries: tuple = field(repr=False)
    preparation_method: str
    source: UploadSource | UploadMemberSource | SelectedPastedSource | None = field(default=None, repr=False)
    interpretation: bytes | None = field(default=None, repr=False)
    output: OutputSettings = field(default=DEFAULT_OUTPUT, kw_only=True)
