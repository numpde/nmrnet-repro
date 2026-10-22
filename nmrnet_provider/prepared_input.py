"""Freeze source selections and scientific inputs without API dependencies.

Optional interpretation bytes are the trusted chooser's secret-free observation
projection. They describe its work; they are not model arguments or authority.
"""

from tools.container_guard import require_container

require_container()

import hashlib
from dataclasses import dataclass, field

from nmrnet_provider.input_geometry import UploadSource
from nmrnet_provider.smiles import GeneratedConformers

DEFAULT_SHIFT_SIGNIFICANT_DIGITS = 4


def require_shift_significant_digits(value):
    if type(value) is not int or not 1 <= value <= 17:
        raise ValueError("The shift_significant_digits field must be an integer from 1 to 17.")
    return value


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
    shift_significant_digits: int = field(default=DEFAULT_SHIFT_SIGNIFICANT_DIGITS, kw_only=True)


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
    source: UploadSource | SelectedPastedSource | None = field(default=None, repr=False)
    interpretation: bytes | None = field(default=None, repr=False)
    shift_significant_digits: int = field(default=DEFAULT_SHIFT_SIGNIFICANT_DIGITS, kw_only=True)
