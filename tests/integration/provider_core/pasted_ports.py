"""Verified pasted Job text with real pure readers and recording lifecycle ports."""

import json
from dataclasses import replace

from tests.fakes.attempt_store import admission, record
from tests.fakes.job_input import facts, response
from tests.integration.nmrnet.smiles_ports import SessionPorts

from nmrnet_provider.geometry_protocol import prepared_reply, read_source, source_request
from provider_core.attempt_record import acknowledge_start
from provider_core.job_input import verify_job_input


class PastedPorts(SessionPorts):
    def __init__(self, source, *, source_format, source_unit):
        super().__init__()
        prefix = "é: coordinates\n".encode()
        raw = prefix + source + b"\nPredict the supplied atoms."
        self.instructions = verify_job_input(response(raw), feed=facts(raw))
        self.start_byte, self.end_byte = len(prefix), len(prefix) + len(source)
        self.source_format, self.source_unit = source_format, source_unit
        self.reader.read.side_effect = self.read
        self.preparation = self.owner(self.choose)
        self.pending = replace(record(), input=self.instructions.identity)
        self.acknowledged = acknowledge_start(self.pending, admission())
        self.session = self.make_session(self.preparation)

    async def read(self, source, **options):
        records = read_source(source_request(source, **options))
        reply = json.loads(json.dumps({"kind": "geometries", "allocation": "fixture", "records": records}))
        return prepared_reply(reply, source, **options)

    async def choose(self, instructions, capability):
        if instructions is not self.instructions:
            raise AssertionError("Pasted selection must use the original verified Job input")
        reference = await capability.prepare_pasted(
            start_byte=self.start_byte,
            end_byte=self.end_byte,
            source_format=self.source_format,
            source_unit=self.source_unit,
        )
        capability.submit(reference)
