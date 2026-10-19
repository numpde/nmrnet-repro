"""Freeze source selections and supplied geometries without API dependencies."""

from tools.container_guard import require_container

require_container()

import hashlib
from dataclasses import dataclass, field

from nmrnet_provider.input_geometry import UploadSource


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
class PreparedJobInput:
    """Supplied coordinates; ResultPlan owns their final scientific admission.

    Generated input uses GeneratedConformers directly, retaining its real atom
    origins instead of inventing file records or supplied-coordinate metadata.
    """

    geometries: tuple = field(repr=False)
    preparation_method: str
    source_upload: UploadSource | None = field(default=None, repr=False)
