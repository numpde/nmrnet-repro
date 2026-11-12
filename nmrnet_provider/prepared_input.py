"""Freeze source selections, scientific inputs and output settings without API I/O.

Optional interpretation bytes are the trusted chooser's secret-free observation
projection. They describe its work; they are not model arguments or authority.
"""

from tools.container_guard import require_container

require_container()

import hashlib
import logging
from dataclasses import dataclass, field, fields

from nmrnet_provider.input_geometry import UploadMemberSource, UploadSource
from nmrnet_provider.smiles import GeneratedConformers
from provider_core.result_verbosity import ResultVerbosity

# NMRNet owns this default presentation precision for omitted output settings.
# repo-check: allow-module-constant
DEFAULT_SHIFT_SIGNIFICANT_DIGITS = 4
# repo-check: allow-module-constant
(MIN_SHIFT_SIGNIFICANT_DIGITS, MAX_SHIFT_SIGNIFICANT_DIGITS) = (1, 10)

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class OutputSettings:
    """Output preferences shared by all preparation routes."""

    verbosity: ResultVerbosity = ResultVerbosity.MEDIUM
    shift_significant_digits: int = DEFAULT_SHIFT_SIGNIFICANT_DIGITS

    def __post_init__(self):
        for f in fields(self):
            if not (type(value := getattr(self, f.name)) is f.type):
                raise TypeError(f"Unexpected {f.name} {type(value) = }")

        (min, max) = (MIN_SHIFT_SIGNIFICANT_DIGITS, MAX_SHIFT_SIGNIFICANT_DIGITS)

        if not (min <= self.shift_significant_digits <= max):
            raise ValueError(f"Expected: {min} <= {self.shift_significant_digits=} <= {max}")

    @classmethod
    def from_json(cls, settings_json: dict):
        if not (type(settings_json) is dict):
            raise TypeError(f"Unexpected {type(settings_json) = }")

        defaults = { f.name: f.default for f in fields(cls) }
        settings = { **defaults, **settings_json }

        if unknown := settings.keys() - defaults.keys():
            log.warning("Ignoring unknown output settings: %s", unknown)

            for name in unknown:
                del settings[name]

        if "verbosity" in settings_json:
            settings["verbosity"] = ResultVerbosity.parse(settings["verbosity"])

        return cls(**settings)

    def as_dict(self):
        serialized = {
            "verbosity": str(self.verbosity),
            "shift_significant_digits": int(self.shift_significant_digits),
        }

        assert set(serialized) == {f.name for f in fields(self)}

        return serialized


# Frozen output settings supply the same presentation defaults to every input path.
# repo-check: allow-module-constant
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
