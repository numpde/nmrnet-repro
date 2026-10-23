"""Observe fixed selected-model input decisions without API Jobs or science.

The cases are local authored instructions and inert attachment metadata. The
real InputInterpreter either reports an ambiguity or chooses Upload arguments.
This owner supplies no preparation feedback that could coach a later decision.
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
from nmrnet_provider.geometry_protocol import require_read_options
from nmrnet_provider.interpreter import InputInterpreter, ModelReportedInputProblem
from nmrnet_provider.process import _diagnostics_directory, _failure_report, _publish
from nmrnet_provider.process_reports import failure_evidence, operator_redactor
from nmrnet_provider.recovery import _run_signalled
from nmrnet_provider.startup_capture import _authority
from provider_core.chat import ChatEndpoint
from provider_core.job_input import INPUT_SCHEMA, FeedInputFacts, VerifiedJobInput
from provider_core.upload_response import UploadMember, UploadSet
from provider_core.uploads import UploadInputRejected


def _encoded(document):
    return (json.dumps(document, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "ascii"
    )


_CASE_COUNTS = {"ambiguity": 4, "formats": 3}


def batch_budget(settings, *, probe="ambiguity"):
    """Cover every admitted turn, its close, and a final retirement per case.

    ChatEndpoint.complete includes connection close; a timed-out operation can
    also consume an outer retirement. Held cleanup still requires exact host
    retirement, rather than treating either elapsed budget as cleanup success.
    """
    endpoint = settings.endpoints[0]
    try:
        seconds = math.ceil(
            _CASE_COUNTS[probe]
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


def admit_selection(fragment, *, selection, key_file, ca_file=None, probe="ambiguity"):
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
        "probe": probe,
        "case_count": _CASE_COUNTS[probe],
        **batch_budget(settings, probe=probe),
    }
    return settings, profile


def load_cases(path, *, probe="ambiguity"):
    """Read the bounded reviewed fixture; its hash accompanies every decision."""
    # This public source fixture has a read-only source grant, rather than a
    # private startup-file grant. Its checkout's ambient group mode is unrelated
    # to credential admission; retain the finite read without imposing that mode.
    with path.open("rb") as source:
        raw = source.read(65537)
    if len(raw) > 65536:
        raise ConfigurationRejected("the authored interpreter fixture must fit 65536 bytes.")
    try:
        document = json.loads(raw)
    except (UnicodeError, ValueError) as cause:
        raise ConfigurationRejected("the authored interpreter fixture must contain valid UTF-8 JSON.", cause=cause) from None
    cases = document.get("cases") if type(document) is dict else None
    fields = {"id", "expected_issue", "specification", "attachment"}
    if probe == "formats":
        fields = {"id", "filename", "byte_length", "sha256", "source_format", "source_unit", "selection", "specification"}
    if type(cases) is not list or any(type(case) is not dict or set(case) != fields for case in cases):
        raise ConfigurationRejected("the authored interpreter fixture must contain complete local cases for this probe.")
    if len(cases) != _CASE_COUNTS[probe] or len({case["id"] for case in cases}) != _CASE_COUNTS[probe]:
        raise ConfigurationRejected("the interpreter fixture has the wrong number of distinct authored cases.")
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

    async def inspect_upload(self, upload_ref):
        """Return authored direct-file metadata without acquiring or parsing bytes.

        Each fixed attachment describes a direct-file Upload. Hash and declared
        length remain authored metadata, not observed transfer evidence.
        No record contents or expected problem category are supplied to the model.
        """
        observed = await self.inspect()
        member = next((entry for entry in observed.members if entry.upload_ref == upload_ref), None)
        if member is None:
            raise UploadInputRejected("selected Upload is not listed for this Job; inspect and select a listed Upload")
        return {
            "job_ref": observed.job_ref,
            "upload_ref": member.upload_ref,
            "byte_length": member.byte_length,
            "content_hash": member.content_hash,
            "container": "direct",
            "members": [],
        }

    def _forbidden(self, name):
        self.preparation_attempts.append(name)
        raise RuntimeError(
            "The interpreter probe called an unsupported preparation tool; no preparation or scientific work was performed."
        )

    async def prepare_smiles(self, **_arguments):
        self._forbidden("prepare_smiles")

    async def prepare_pasted(self, **_arguments):
        self._forbidden("prepare_pasted")

    async def prepare_upload(self, **_arguments):
        self._forbidden("prepare_upload")


class _FormatChoiceRejected(RuntimeError):
    """Code-owned comparison guidance, retained separately from model arguments."""


class _FormatChoiceOnly(_InspectionOnly):
    """Capture terminal Upload arguments; return no geometry or prepared handle.

    InputInterpreter ends the conversation when prepare_upload returns. Nothing
    is sent back to coach another choice. Check the captured arguments afterwards,
    using the real reader's pre-resource predicate without executing the reader.
    """

    def __init__(self, instructions, case):
        super().__init__(instructions, case)
        self.arguments = None

    async def inspect(self):
        return UploadSet(
            self.instructions.job_ref,
            (
                UploadMember(
                    "upload:sha256:" + "1" * 64, self.case["filename"], self.case["byte_length"], "sha256:" + self.case["sha256"]
                ),
            ),
        )

    async def prepare_upload(self, **arguments):
        self.preparation_attempts.append("prepare_upload")
        self.arguments = _encoded(arguments)

    def require_choice(self):
        if self.arguments is None:
            raise _FormatChoiceRejected("The interpreter did not choose the attached file through prepare_upload.")
        arguments = json.loads(self.arguments)
        selection = arguments["selection"]
        if type(selection) is list:
            selection = tuple(selection)
        require_read_options(arguments["source_format"], arguments["source_unit"], selection, arguments.get("alternate_location"))
        # models.pdb has only blank alternate sites. _selected_rows in pdb.py
        # preserves those rows with any admitted label; don't reject an option
        # that changes no selected atom. Other formats require None above.
        if (
            arguments["upload_ref"] != "upload:sha256:" + "1" * 64
            or arguments["source_format"] != self.case["source_format"]
            or arguments["source_unit"] != self.case["source_unit"]
            or arguments["selection"] != self.case["selection"]
            or arguments.get("member") is not None
        ):
            raise _FormatChoiceRejected(
                "The observed Upload choice differs from the authored file, format, units or record order."
            )


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


async def run_batch(settings, cases, *, output, configuration_sha256, probe="ambiguity"):
    """Run each fixed case once and stop at the first refusal or operational error."""
    secrets = (settings.endpoints[0].spec.api_key,)
    passed = 0
    for case in cases:
        raw = case["specification"].encode("utf-8")
        instructions = VerifiedJobInput(
            FeedInputFacts(
                "job:" + probe + "-" + case["id"], INPUT_SCHEMA, "sha256:" + hashlib.sha256(raw).hexdigest(), len(raw)
            ),
            raw,
        )
        capability = _FormatChoiceOnly(instructions, case) if probe == "formats" else _InspectionOnly(instructions, case)
        endpoint = settings.endpoints[0]
        carrier = ChatEndpoint(
            endpoint.spec,
            transport=endpoint.http.new_transport(),
            request_bytes=endpoint.request_bytes,
            response_bytes=endpoint.response_bytes,
        )
        interpreter = InputInterpreter if probe == "formats" else _ProblemOnlyInterpreter
        chooser = interpreter((carrier,), max_turns=settings.max_turns)
        observed = {
            "case_id": case["id"],
            "configuration_sha256": configuration_sha256,
            "case_sha256": hashlib.sha256(_encoded(case)).hexdigest(),
            "input_fingerprint": instructions.identity.input_fingerprint,
            "input_byte_length": len(raw),
            "authenticated_api_job": False,
            "attachment_metadata_authored": probe == "formats" or case["attachment"],
            "independently_verified": False,
        }
        if probe == "formats":
            observed.update(evidence_scope="prepare_upload_arguments", scientific_preparation_performed=False)
        else:
            observed["expected_issue"] = case["expected_issue"]
        failures = []
        try:
            interpretation = await chooser.run(instructions, capability)
            if probe == "formats":
                observed["interpretation"] = json.loads(interpretation)
                capability.require_choice()
            else:
                failures.append(("model decision", RuntimeError("The model did not report the authored input problem.")))
        except _FormatChoiceRejected as cause:
            observed["choice_refusal"] = str(cause)
            failures.append(("model argument choice", cause))
        except ModelReportedInputProblem as report:
            observed.update(
                issue=report.issue,
                guidance=str(report),
                explanation=report.explanation,
                interpretation=json.loads(report.observation),
            )
            if probe == "formats" or report.issue != case["expected_issue"]:
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
            if probe == "formats" and capability.arguments is not None:
                observed["tool_arguments_json"] = operator_redactor(secrets=secrets)(capability.arguments.decode("ascii"))
            if capability.interpreter_diagnostics is not None:
                observed["interpreter_diagnostics"] = json.loads(capability.interpreter_diagnostics)
            observed["passed"] = not failures and (probe == "formats" or not capability.preparation_attempts)
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
            {
                "passed": passed == _CASE_COUNTS[probe],
                "cases_passed": passed,
                "authenticated_api_jobs": False,
                "scientific_work_performed": False,
                "evidence_scope": "prepare_upload_arguments" if probe == "formats" else "input_problem_decisions",
            }
        ),
    )
    return 0 if passed == _CASE_COUNTS[probe] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("admit", "run"))
    parser.add_argument("--probe", choices=tuple(_CASE_COUNTS), default="ambiguity")
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
            args.interpreter_config, selection=args.selection, key_file=args.key_file, ca_file=args.ca_file, probe=args.probe
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
                    settings,
                    load_cases(args.cases, probe=args.probe),
                    output=args.output,
                    configuration_sha256=profile["configuration_sha256"],
                    probe=args.probe,
                )

        return asyncio.run(_run_signalled(run()))
    except (Exception, asyncio.CancelledError) as cause:
        _failure_report((("selected-model interpreter probe", cause),), secrets=secrets, held=False)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
