"""Assemble one interpreted NMRNet Attempt from existing lifetime owners.

Startup assembly follows SECS 7356c354368f2355d3ab5cef00cf48c67cea9c2c,
src/secs_inference/provider/main.py:67-105, and NMRPeak
1697dc84dd1d76ae25b04efc8183fd80614000e5,
nmrpeak_provider/provider_main.py:71-147. Core's controller already owns the
start/execute/retire/publish sequence; this module only wires admitted owners.
No service loop, container engine, recovery journal or alternative JSON binding.
"""

from tools.container_guard import require_container

require_container()

from nmrnet_provider.config import ANALYSIS_KIND_REF, ProviderConfig
from nmrnet_provider.input_preparation import InputPreparation
from nmrnet_provider.interpreter import InputInterpreter
from nmrnet_provider.session import NMRNetSession
from provider_core.api import ProviderAPI
from provider_core.attempt_record import StartPending, TerminalConfirmed
from provider_core.chat import ChatEndpoint
from provider_core.controller import AttemptController, ControllerRefused
from provider_core.uploads import JobUploads


async def assemble_attempt(
    *,
    config,
    pending,
    api,
    store,
    setup_owner,
    claims,
    execution_identity,
    selected_artifacts,
    allocate_prediction,
    on_prediction_retry,
    on_interpreter_failure,
    allocate_reader,
    allocate_generator,
    observation_recovery,
    condition_recovery,
    on_unconfirmed_phase_progress,
    start_recovery,
    terminal_recovery,
):
    """Return the existing controller, which must run this same selected pending.

    The caller obtains pending.input from authenticated ProviderAPI.jobs and owns
    the opened store and lifecycle API. Prediction is acquired only after START
    acknowledgement and successful input preparation. Reader/generator factories
    keep partial allocations until they return their client/retirement pair.

    Inventory is an early refusal check, not a claim: core checks again at run.
    setup_owner identifies the caller's inventory read; explicit None keeps an
    externally serialized setup read. Cancellation/timeout can leave that original
    read pending; its owner must settle it before retry or close. No allocation or API request
    occurs here; all construction after the inventory await is passive/synchronous.
    """
    if type(config) is not ProviderConfig or type(pending) is not StartPending:
        raise ValueError("Cannot assemble NMRNet Attempt: admitted configuration and selected pending start are required.")
    if not callable(on_interpreter_failure):
        raise ValueError("Cannot assemble NMRNet Attempt: interpreter failures require an operator diagnostic recipient.")
    if api.identity is not config.api.identity:
        raise ValueError("Cannot assemble NMRNet Attempt: lifecycle API must use the configured signing identity.")
    if api.expected_topology != config.api.expected_topology:
        raise ValueError("Cannot assemble NMRNet Attempt: lifecycle API must use the configured API topology.")
    if store.scope != config.lifecycle.scope or pending.scope != store.scope:
        raise ValueError(
            "Cannot assemble NMRNet Attempt: configuration, retained store and pending start must share a recovery scope."
        )
    if pending.analysis_kind_ref != ANALYSIS_KIND_REF:
        raise ValueError("Cannot assemble NMRNet Attempt: the selected Job must request peaks_from_mol.")
    if any(type(record) is not TerminalConfirmed for record in await store.records(owner=setup_owner)):
        raise ControllerRefused("Cannot assemble new work: retained Attempt obligations require explicit recovery first.")

    source_api_transport = config.api.http.new_transport()
    source_store_transport = config.uploads.http.new_transport()
    source_api = ProviderAPI(config.api.identity, source_api_transport, expected_topology=config.api.expected_topology)
    uploads = JobUploads(
        pending.input.job_ref,
        api=source_api,
        store=source_store_transport,
        store_origin=config.uploads.store_origin,
        limits=config.uploads.limits,
    )
    endpoints = tuple(
        ChatEndpoint(
            settings.spec,
            transport=settings.http.new_transport(),
            request_bytes=settings.request_bytes,
            response_bytes=settings.response_bytes,
        )
        for settings in config.interpreter.endpoints
    )
    chooser = InputInterpreter(
        endpoints, max_turns=config.interpreter.max_turns, on_endpoint_failure=on_interpreter_failure
    )
    settings = config.preparation
    preparation = InputPreparation(
        uploads=uploads,
        source_api_transport=source_api_transport,
        source_store_transport=source_store_transport,
        allocate_reader=allocate_reader,
        allocate_generator=allocate_generator,
        generation_settings=settings.generation_settings,
        generation_seconds=settings.generation_seconds,
        chooser=chooser,
        max_worker_calls=settings.max_worker_calls,
        seconds=settings.seconds,
    )
    session = NMRNetSession(
        allocate_prediction=allocate_prediction,
        execution_identity=execution_identity,
        selected_artifacts=selected_artifacts,
        input_preparation=preparation,
        on_prediction_retry=on_prediction_retry,
    )
    settings = config.lifecycle
    return AttemptController(
        api,
        store,
        claims,
        session,
        allowed_job_states=settings.allowed_job_states,
        poll_seconds=settings.poll_seconds,
        task_settle_seconds=settings.task_settle_seconds,
        observation_recovery=observation_recovery,
        condition_recovery=condition_recovery,
        on_unconfirmed_phase_progress=on_unconfirmed_phase_progress,
        start_recovery=start_recovery,
        terminal_recovery=terminal_recovery,
    )
