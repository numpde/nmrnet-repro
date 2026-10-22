"""Qualify four selected-model input decisions without API Jobs or scientific work.

The cases are local authored instructions and inert attachment metadata. The
binding's real InputInterpreter decides whether to report a problem; this owner
never supplies preparation feedback that could coach it into the expected report.
Existing config, chat, interpretation and private process-report owners retain
their admission, byte limits, redaction and connection-retirement responsibilities.
"""

from tools.container_guard import require_container

require_container()

import argparse
import asyncio
import hashlib
import json
import math
from dataclasses import replace
from pathlib import Path

import tomllib
from stage_api_config import interpreter_fragment

from nmrnet_provider.config import ConfigurationRejected, _interpreter, read_startup_file
from nmrnet_provider.interpreter import InputInterpreter, ModelReportedInputProblem
from nmrnet_provider.process import _diagnostics_directory, _failure_report, _publish
from nmrnet_provider.process_reports import failure_evidence
from nmrnet_provider.recovery import _run_signalled
from nmrnet_provider.startup_capture import _authority
from provider_core.chat import ChatEndpoint
from provider_core.job_input import INPUT_SCHEMA, FeedInputFacts, VerifiedJobInput
from provider_core.upload_response import UploadMember, UploadSet


def _encoded(document):
    return (json.dumps(document, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "ascii"
    )


def batch_budget(settings):
    """Cover every admitted turn, its close, and a final retirement per case.

    ChatEndpoint.complete includes connection close; a timed-out operation can
    also consume an outer retirement. Held cleanup still requires exact host
    retirement, rather than treating either elapsed budget as cleanup success.
    """
    endpoint = settings.endpoints[0]
    try:
        seconds = math.ceil(
            4
            * (
                settings.max_turns * (endpoint.http.exchange_seconds + endpoint.http.retirement_seconds)
                + endpoint.http.retirement_seconds
            )
            + 60
        )
    except (ValueError, OverflowError) as cause:
        raise ConfigurationRejected("the selected interpreter batch must fit finite elapsed seconds.", cause=cause) from None
    if not 1 <= seconds <= 999979:
        raise ConfigurationRejected("the selected interpreter batch exceeds the finite host clock range.")
    return {"process_seconds": seconds, "host_seconds": seconds + 20}


def admit_selection(fragment, *, selection, key_file, ca_file=None):
    """Admit one unchanged fragment using only its individually selected grants."""
    try:
        table, inputs = interpreter_fragment(fragment)
    except (UnicodeError, tomllib.TOMLDecodeError) as cause:
        raise ConfigurationRejected("the interpreter fragment must contain valid UTF-8 TOML.", cause=cause) from None
    except ValueError as cause:
        # The fragment owner's remaining ValueErrors contain fixed local copy.
        if isinstance(cause, ConfigurationRejected):
            raise
        raise ConfigurationRejected(str(cause), cause=cause) from None
    try:
        selected = json.loads(read_startup_file(selection, maximum_bytes=65536, label="interpreter file selection", secret=True))
    except (UnicodeError, ValueError) as cause:
        if isinstance(cause, ConfigurationRejected):
            raise
        raise ConfigurationRejected("the interpreter file-selection receipt must contain valid JSON.", cause=cause) from None
    if inputs != selected:
        raise ConfigurationRejected("the interpreter configuration changed after its individual file grants were selected.")
    if (inputs["ca_file"] is None) != (ca_file is None):
        raise ConfigurationRejected("the explicit CA grant must match the selected interpreter fragment.")
    grants = {inputs["key_file"]: key_file}
    if ca_file is not None:
        grants[inputs["ca_file"]] = ca_file

    def read_grant(path, **limits):
        return read_startup_file(grants[str(path)], **limits)

    try:
        settings = _interpreter(table, read_grant)
    except ConfigurationRejected:
        raise
    except OverflowError as cause:
        raise ConfigurationRejected(
            "configured time limits must fit the runtime's finite seconds representation.", cause=cause
        ) from None
    except ValueError as cause:
        # Match the existing full configuration owner's reviewed validator boundary.
        raise ConfigurationRejected(str(cause), cause=cause) from None
    endpoint = settings.endpoints[0]
    profile = {
        "configuration_sha256": inputs["configuration_sha256"],
        "authority": _authority(endpoint.spec.url),
        "endpoint": json.loads(
            ChatEndpoint(
                endpoint.spec,
                transport=endpoint.http.new_transport(),
                request_bytes=endpoint.request_bytes,
                response_bytes=endpoint.response_bytes,
            ).identity
        ),
        "max_turns": settings.max_turns,
        "request_bytes": endpoint.request_bytes,
        "response_bytes": endpoint.response_bytes,
        "exchange_seconds": endpoint.http.exchange_seconds,
        "retirement_seconds": endpoint.http.retirement_seconds,
        **batch_budget(settings),
    }
    return settings, profile


def load_cases(path):
    """Read the bounded reviewed fixture; its hash accompanies every decision."""
    # This public source fixture has a read-only source grant, rather than a
    # private startup-file grant. Its checkout's ambient group mode is unrelated
    # to credential admission; retain the finite read without imposing that mode.
    with path.open("rb") as source:
        raw = source.read(65537)
    if len(raw) > 65536:
        raise ConfigurationRejected("the authored ambiguity fixture must fit 65536 bytes.")
    try:
        document = json.loads(raw)
    except (UnicodeError, ValueError) as cause:
        raise ConfigurationRejected("the authored ambiguity fixture must contain valid UTF-8 JSON.", cause=cause) from None
    cases = document.get("cases") if type(document) is dict else None
    if type(cases) is not list or any(
        type(case) is not dict or set(case) != {"id", "expected_issue", "specification", "attachment"} for case in cases
    ):
        raise ConfigurationRejected("the authored ambiguity fixture must contain the four complete local cases.")
    if len(cases) != 4 or len({case["id"] for case in cases}) != 4:
        raise ConfigurationRejected("the ambiguity fixture requires four distinct authored cases.")
    return cases


class _InspectionOnly:
    """Expose authored metadata and fail immediately at every preparation boundary."""

    def __init__(self, instructions, case):
        self.instructions, self.case = instructions, case
        self.preparation_attempts = []
        self.interpreter_diagnostics = None

    def retain_interpreter_diagnostics(self, content):
        if type(content) is not bytes:
            raise TypeError("Interpreter diagnostics must be immutable JSON bytes.")
        self.interpreter_diagnostics = content

    async def inspect(self):
        members = ()
        if self.case["attachment"]:
            members = (
                UploadMember(
                    "upload:sha256:" + "1" * 64,
                    "mixed_records.xyz; two XYZ records; coordinates in angstrom",
                    512,
                    "sha256:" + "2" * 64,
                ),
            )
        return UploadSet(self.instructions.job_ref, members)

    def _forbidden(self, name):
        self.preparation_attempts.append(name)
        raise RuntimeError(
            "Ambiguity qualification attempted molecular preparation; no preparation or scientific work was performed."
        )

    async def prepare_smiles(self, **_arguments):
        self._forbidden("prepare_smiles")

    async def prepare_pasted(self, **_arguments):
        self._forbidden("prepare_pasted")

    async def prepare_upload(self, **_arguments):
        self._forbidden("prepare_upload")


class _ProblemOnlyInterpreter(InputInterpreter):
    """Reject preparation names before argument feedback can coach a retry.

    The production prompt and tool declarations stay unchanged. Raw malformed
    JSON remains with the core parser; corrected conversations cannot qualify.
    """

    async def _dispatch(self, capability, name, arguments):
        if name in ("prepare_smiles", "prepare_pasted", "prepare_upload"):
            capability._forbidden(name)
        return await super()._dispatch(capability, name, arguments)


async def _hold():
    while True:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            # A subsequent signal cannot release the original unresolved owner.
            continue


async def run_batch(settings, cases, *, output, configuration_sha256):
    """Run each fixed case once and stop at the first refusal or operational error."""
    secrets = (settings.endpoints[0].spec.api_key,)
    passed = 0
    for case in cases:
        raw = case["specification"].encode("utf-8")
        instructions = VerifiedJobInput(
            FeedInputFacts("job:ambiguity-" + case["id"], INPUT_SCHEMA, "sha256:" + hashlib.sha256(raw).hexdigest(), len(raw)),
            raw,
        )
        capability = _InspectionOnly(instructions, case)
        endpoint = settings.endpoints[0]
        carrier = ChatEndpoint(
            endpoint.spec,
            transport=endpoint.http.new_transport(),
            request_bytes=endpoint.request_bytes,
            response_bytes=endpoint.response_bytes,
        )
        chooser = _ProblemOnlyInterpreter((carrier,), max_turns=settings.max_turns)
        observed = {
            "case_id": case["id"],
            "expected_issue": case["expected_issue"],
            "configuration_sha256": configuration_sha256,
            "case_sha256": hashlib.sha256(_encoded(case)).hexdigest(),
            "input_fingerprint": instructions.identity.input_fingerprint,
            "input_byte_length": len(raw),
            "authenticated_api_job": False,
            "attachment_metadata_authored": case["attachment"],
            "independently_verified": False,
        }
        failures = []
        try:
            await chooser.run(instructions, capability)
            failures.append(("model decision", RuntimeError("The model did not report the authored input problem.")))
        except ModelReportedInputProblem as report:
            observed.update(
                issue=report.issue,
                guidance=str(report),
                explanation=report.explanation,
                interpretation=json.loads(report.observation),
            )
            if report.issue != case["expected_issue"]:
                failures.append(
                    ("model decision", RuntimeError("The model reported a different category from the authored case."))
                )
            if observed["interpretation"]["corrections"] != 0:
                failures.append(
                    ("model decision", RuntimeError("A corrected conversation does not qualify an uncoached ambiguity decision."))
                )
        except (Exception, asyncio.CancelledError) as cause:
            failures.append(("input interpretation", cause))
        finally:
            retirement = asyncio.create_task(chooser.retire())
            while not retirement.done():
                try:
                    await asyncio.shield(retirement)
                except asyncio.CancelledError as cause:
                    failures.append(("batch interruption during model retirement", cause))
                except Exception:
                    # The original task below retains the exact cleanup cause.
                    break
            cleanup = None
            try:
                retirement.result()
            except (Exception, asyncio.CancelledError) as cause:
                cleanup = cause
                failures.append(("model connection retirement", cause))
            observed["preparation_attempts"] = capability.preparation_attempts
            if capability.interpreter_diagnostics is not None:
                observed["interpreter_diagnostics"] = json.loads(capability.interpreter_diagnostics)
            observed["passed"] = not failures and not capability.preparation_attempts
            observed["requires_host_retirement"] = cleanup is not None
            if failures:
                observed["failures"] = [
                    {"operation": operation, "failure": failure_evidence(cause, secrets=secrets)} for operation, cause in failures
                ]
            try:
                _publish(output, case["id"] + ".json", _encoded(observed))
                if cleanup is not None:
                    _failure_report(failures, secrets=secrets, held=True)
            finally:
                if cleanup is not None:
                    await _hold()
        if not observed["passed"]:
            break
        passed += 1
    _publish(
        output,
        "result.json",
        _encoded(
            {"passed": passed == 4, "cases_passed": passed, "authenticated_api_jobs": False, "scientific_work_performed": False}
        ),
    )
    return 0 if passed == 4 else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("admit", "run"))
    parser.add_argument("--interpreter-config", required=True, type=Path)
    parser.add_argument("--selection", required=True, type=Path)
    parser.add_argument("--key-file", required=True, type=Path)
    parser.add_argument("--ca-file", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--proxy-socket")
    parser.add_argument("--cases", type=Path)
    args = parser.parse_args()
    _diagnostics_directory(args.output)
    secrets = ()
    try:
        settings, profile = admit_selection(
            args.interpreter_config, selection=args.selection, key_file=args.key_file, ca_file=args.ca_file
        )
        secrets = (settings.endpoints[0].spec.api_key,)
        if args.operation == "admit":
            if any(value is not None for value in (args.profile, args.proxy_socket, args.cases)):
                parser.error("admission does not accept a route, cases or earlier profile")
            _publish(args.output, "profile.json", _encoded(profile))
            _publish(args.output, "authority.txt", (profile["authority"] + "\n").encode("ascii"))
            _publish(args.output, "host-seconds.txt", (str(profile["host_seconds"]) + "\n").encode("ascii"))
            return 0
        if args.profile is None or args.proxy_socket is None or args.cases is None:
            parser.error("run requires its admitted profile, sole Unix proxy and fixed cases")
        earlier = json.loads(read_startup_file(args.profile, maximum_bytes=65536, label="interpreter batch profile"))
        if earlier != profile:
            raise ConfigurationRejected("the admitted interpreter batch profile changed before execution.")
        endpoint = settings.endpoints[0]
        settings = replace(settings, endpoints=(replace(endpoint, http=replace(endpoint.http, proxy_socket=args.proxy_socket)),))

        async def run():
            async with asyncio.timeout(profile["process_seconds"]):
                return await run_batch(
                    settings, load_cases(args.cases), output=args.output, configuration_sha256=profile["configuration_sha256"]
                )

        return asyncio.run(_run_signalled(run()))
    except (Exception, asyncio.CancelledError) as cause:
        _failure_report((("interpreter ambiguity qualification", cause),), secrets=secrets, held=False)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
