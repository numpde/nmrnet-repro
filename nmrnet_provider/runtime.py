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
from provider_core.attempt_record import StartPending
from provider_core.chat import ChatEndpoint
from provider_core.controller import AttemptController
from provider_core.delivery_client import DeliveryClient
from provider_core.uploads import JobUploads, UploadRecovery


async def assemble_attempt(
    *,
    config,
    pending,
    claims,
    stopping,
    admission_stopping,
    start_policy,
    execution_identity,
    selected_artifacts,
    allocate_prediction,
    on_prediction_failure,
    on_interpreter_failure,
    on_source_retry,
    on_source_observation,
    allocate_reader,
    allocate_generator,
    observation_recovery,
    condition_recovery,
    on_unconfirmed_phase_progress,
    on_delivery_retry,
):
    """Return the existing controller, which must run this same selected pending.

    The caller obtains pending.input from authenticated ProviderAPI.jobs.
    This controller receives its own API and delivery exchanges; a held Attempt
    cannot retire the exchanges of a later unrelated Job. Prediction follows START
    acknowledgement and successful input preparation. Reader/generator factories
    keep partial allocations until they return their client/retirement pair.

    Construction allocates no worker and sends no request. The shared controller
    reserves live capacity and Job/key ownership before sending START.
    """
    if type(config) is not ProviderConfig or type(pending) is not StartPending:
        raise ValueError("Cannot assemble NMRNet Attempt: admitted configuration and selected pending start are required.")
    if not callable(on_interpreter_failure):
        raise ValueError("Cannot assemble NMRNet Attempt: interpreter failures require an operator diagnostic recipient.")
    if not callable(on_prediction_failure):
        raise ValueError("Cannot assemble NMRNet Attempt: prediction failures require an operator diagnostic recipient.")
    if pending.scope != config.lifecycle.scope:
        raise ValueError("Cannot assemble NMRNet Attempt: selection differs from the configured provider scope.")
    if pending.analysis_kind_ref != ANALYSIS_KIND_REF:
        raise ValueError("Cannot assemble NMRNet Attempt: the selected Job must request peaks_from_mol.")
    api = ProviderAPI(config.api.identity, config.api.http.new_transport(),
                      expected_topology=config.api.expected_topology)
    source_api_transport = config.api.http.new_transport()
    source_store_transport = config.uploads.http.new_transport()
    source_api = ProviderAPI(config.api.identity, source_api_transport, expected_topology=config.api.expected_topology)
    uploads = JobUploads(
        pending.input.job_ref,
        api=source_api,
        store=source_store_transport,
        store_origin=config.uploads.store_origin,
        limits=config.uploads.limits,
        recovery=UploadRecovery(
            maximum_attempts=3,
            initial_seconds=config.service.policy.idle_seconds,
            maximum_seconds=config.service.policy.api_backoff_max_seconds,
            stopping=stopping,
            on_retry=on_source_retry,
        ),
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
        on_source_observation=on_source_observation,
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
        on_prediction_failure=on_prediction_failure,
        max_retries=config.scientific_max_retries,
        prediction_seconds=config.service.native.prediction_seconds,
    )
    settings = config.lifecycle
    return AttemptController.live(
        api,
        claims,
        session,
        scope=settings.scope,
        stopping=stopping,
        admission_stopping=admission_stopping,
        start_policy=start_policy,
        allow_active_start_replay=True,
        delivery=DeliveryClient(config.delivery.endpoint, exchange_seconds=config.delivery.request_seconds,
                                retirement_seconds=config.delivery.retirement_seconds),
        on_delivery_retry=on_delivery_retry,
        delivery_retry_initial_seconds=config.delivery.retry_initial_seconds,
        delivery_retry_max_seconds=config.delivery.retry_max_seconds,
        allowed_job_states=settings.allowed_job_states,
        poll_seconds=settings.poll_seconds,
        task_settle_seconds=settings.task_settle_seconds,
        observation_recovery=observation_recovery,
        condition_recovery=condition_recovery,
        on_unconfirmed_phase_progress=on_unconfirmed_phase_progress,
    )
