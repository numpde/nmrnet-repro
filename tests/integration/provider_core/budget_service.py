"""One call-limited Job precedes the existing successful/replayed Job fixtures."""

import asyncio
import json
from dataclasses import replace

from tests.fakes.attempts import fail_response
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
        self.failure_bodies = []

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

    def failure_receipt(self, index, value, pending):
        if index != 0 or value["failure_code"] != "input_worker_call_limit":
            raise AssertionError("Only the first Job's owned worker-call exhaustion may become FAIL")
        if "1" not in value["failure_message"] or "operator" not in value["failure_message"]:
            raise AssertionError("Failure recipient did not receive the configured limit and next action")
        if any(token in value["failure_message"] for token in (self.source_text.strip(), "PRIVATE", "invalid_input")):
            raise AssertionError("Failure guidance contains source/private text or mislabels a capacity limit as invalid input")
        evidence = json.loads(pending.evidence.content)
        if evidence["input_worker_call_limit"] != {"maximum_calls": 1, "calls_started": 1}:
            raise AssertionError("Protected failure evidence lost actual per-Job call accounting")
        if not evidence["interpreter_diagnostics"]:
            raise AssertionError("Corrected reader rejection disappeared from the retained interpreter diagnostics")
        self.failure_bodies.append(pending.body)
        return fail_response(
            execution_attempt_ref=self.attempt_refs[index],
            failure_code=value["failure_code"],
            failure_message=value["failure_message"],
        )


class InputTimeLimitPeers(SuccessiveServicePeers):
    """The first Job's model reply stalls; later Jobs use the original fixture."""

    def __init__(self):
        super().__init__(maximum_records=1)
        self.failure_bodies = []

    async def __aenter__(self):
        await super().__aenter__()
        self.config = replace(self.config, preparation=replace(self.config.preparation, seconds=0.2))
        return self

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

    def failure_receipt(self, index, value, pending):
        if index != 0 or value["failure_code"] != "input_selection_time_limit":
            raise AssertionError("Only the first Job's owned selection deadline may become FAIL")
        if "0.2" not in value["failure_message"] or "operator" not in value["failure_message"]:
            raise AssertionError("Failure recipient lost the configured limit or next action")
        if any(token in value["failure_message"] for token in ("PRIVATE", "invalid_input", self.jobs[0].specification_text)):
            raise AssertionError("Time-limit guidance leaked private input or claimed invalid molecular input")
        evidence = json.loads(pending.evidence.content)
        if evidence["input_selection_time_limit"] != {"seconds": 0.2}:
            raise AssertionError("Protected time-limit evidence lost the owned aggregate budget")
        rendered = json.dumps(evidence["interpreter_diagnostics"])
        if "request_cancelled" not in rendered or "may have been processed" not in rendered or "PRIVATE_MODEL_KEY" in rendered:
            raise AssertionError("Time-limit evidence erased model effect uncertainty or exposed its configured key")
        self.failure_bodies.append(pending.body)
        return fail_response(
            execution_attempt_ref=self.attempt_refs[index],
            failure_code=value["failure_code"],
            failure_message=value["failure_message"],
        )
