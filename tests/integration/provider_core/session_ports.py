"""Explicit coordinate-only preparation for scientific session tests.

The tests choose output settings independently of verified scientific JSON. This
port runs no interpreter and owns no source transports or native resources.
"""

from nmrnet_provider.input_geometry import prepare_json
from nmrnet_provider.prepared_input import DEFAULT_OUTPUT, PreparedJobInput


class CoordinateJsonPreparation:
    interpreter_diagnostics = None

    def __init__(self, *, output=DEFAULT_OUTPUT):
        self.output = output

    async def prepare(self, instructions):
        return PreparedJobInput(
            (prepare_json(instructions.specification_bytes),), "direct_coordinate_json", output=self.output
        )

    async def retire(self):
        pass
