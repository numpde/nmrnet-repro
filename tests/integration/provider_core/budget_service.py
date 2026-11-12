"""Retain a budget-limited first Job while later Jobs remain undispatched."""

import asyncio
import json
from dataclasses import replace

from tests.fakes.job_input import facts, response
from tests.integration.nmrnet.service_ports import SuccessiveServicePeers

from nmrnet_provider.input_geometry import InputRejected
from provider_core.job_input import verify_job_input


class InputCallLimitPeers(SuccessiveServicePeers):
    source_text = "H invalid-coordinate 0 0\n"

    def __init__(self):
        super().__init__()
        raw = ("Job A: coordinates in nm:\n" + self.source_text).encode()
        job_ref = self.jobs[0].job_ref
        self.jobs = (verify_job_input(response(raw, job_ref=job_ref), feed=replace(facts(raw), job_ref=job_ref)), *self.jobs[1:])

    async def __aenter__(self):
        await super().__aenter__()
        self.config = replace(self.config, preparation=replace(self.config.preparation, max_worker_calls=1))
        return self

    def scientific_ports(self, pending):
        ports = super().scientific_ports(pending)
        self.route = "smiles"
        if pending.input == self.jobs[0].identity:
            self.route = "pasted"
            ports.source_text = self.source_text
            # Recording reader rejection, not evidence of a native parser.
            ports.reader.read.side_effect = InputRejected("Cannot read coordinate table: x must be a finite number.")
        return ports

    async def model_reply(self, request, raw):
        reply = await super().model_reply(request, raw)
        if self.ports.instructions.identity == self.jobs[0].identity:
            # Each corrective exchange is a distinct model tool call, even
            # when this fixed bad-input fixture repeats its coordinate text.
            document = json.loads(reply)
            document["choices"][0]["message"]["tool_calls"][0]["id"] = f"call-limit-{len(self.model_jobs)}"
            return json.dumps(document).encode()
        return reply

    def require_retired_job(self, ports):
        if ports.instructions.identity != self.jobs[0].identity:
            super().require_retired_job(ports)
            return
        if ports.worker.events or ports.host.confirmed:
            raise AssertionError("Call-limited Job acquired prediction before input succeeded")
        ports.allocate_reader.assert_awaited_once()
        ports.reader.read.assert_awaited_once()
        ports.reader_host.retire.assert_awaited_once()
        ports.reader.close.assert_called_once()
        ports.reader.wait_closed.assert_awaited_once()
        ports.allocate_generator.assert_not_awaited()


class InputTimeLimitPeers(SuccessiveServicePeers):
    """A stalled first Job retains its Attempt before later Jobs can start."""

    selection_seconds = 2.0

    def __init__(self):
        super().__init__(maximum_live_attempts=1)

    def arguments(self, pending):
        arguments = super().arguments(pending)
        if pending.input == self.jobs[0].identity:
            config = arguments["config"]
            arguments["config"] = replace(config, preparation=replace(config.preparation, seconds=self.selection_seconds))
        return arguments

    async def model_reply(self, request, raw):
        response = await super().model_reply(request, raw)
        if self.ports.instructions.identity == self.jobs[0].identity:
            # The peer may already be processing this POST. It never returns a
            # tool; retirement closes the client, without sending a second POST.
            await asyncio.Event().wait()
        return response

    def require_retired_job(self, ports):
        if ports.instructions.identity != self.jobs[0].identity:
            super().require_retired_job(ports)
            return
        if ports.worker.events or ports.host.confirmed:
            raise AssertionError("Time-limited input entered prediction or left its prediction allocation alive")
        ports.allocate_reader.assert_not_awaited()
        ports.allocate_generator.assert_not_awaited()
