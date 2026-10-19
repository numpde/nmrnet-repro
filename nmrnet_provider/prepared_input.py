"""Freeze source selections and supplied geometries without API dependencies."""

from tools.container_guard import require_container

require_container()

import hashlib
from dataclasses import dataclass, field

from nmrnet_provider.input_geometry import UploadSource
from nmrnet_provider.smiles import GeneratedConformers


@dataclass(frozen=True, slots=True)
class SelectedSmilesSource:
    """An interpreter's selection from a verified Job, not proof of prose intent.

    Offsets are a half-open UTF-8 byte range in the original specification. The
    immutable slice belongs to this Attempt; it is not a credential or cache.
    """

    job_ref: str
    input_fingerprint: str
    start_byte: int
    end_byte: int
    requested_count: int
    source_bytes: bytes = field(repr=False)

    @property
    def source_sha256(self):
        return hashlib.sha256(self.source_bytes).hexdigest()


@dataclass(frozen=True, slots=True)
class GeneratedJobInput:
    """Join the Job selection to native output; ResultPlan admits their agreement.

    The input owner selects from verified instructions and confirms generator
    retirement before submitting this value. The pair itself grants no authority.
    """

    source: SelectedSmilesSource
    generated: GeneratedConformers = field(repr=False)


@dataclass(frozen=True, slots=True)
class SelectedPastedSource:
    """An explicit coordinate span in original Job bytes, not inferred intent.

    Reader line numbers are relative to this selected span. Format and unit are
    chooser decisions; the reader owns parsing and conversion to angstroms.
    """

    job_ref: str
    input_fingerprint: str
    start_byte: int
    end_byte: int
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
