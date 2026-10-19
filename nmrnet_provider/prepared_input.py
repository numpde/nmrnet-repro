"""Carry supplied geometries and their acquisition facts without API dependencies."""

from tools.container_guard import require_container

require_container()

from dataclasses import dataclass, field

from nmrnet_provider.input_geometry import UploadSource


@dataclass(frozen=True, slots=True)
class PreparedJobInput:
    """Supplied coordinates; ResultPlan owns their final scientific admission.

    Generated input uses GeneratedConformers directly, retaining its real atom
    origins instead of inventing file records or supplied-coordinate metadata.
    """

    geometries: tuple = field(repr=False)
    preparation_method: str
    source_upload: UploadSource | None = field(default=None, repr=False)
