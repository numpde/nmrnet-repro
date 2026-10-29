"""Load one explicit startup configuration before acquiring runtime resources.

Ports SECS 7356c354368f2355d3ab5cef00cf48c67cea9c2c,
src/secs_inference/provider/config.py:113-165 and main.py:111-151; constructs
existing policy objects as NMRPeak 1697dc84dd1d76ae25b04efc8183fd80614000e5,
nmrpeak_provider/provider_config.py:91-225 does. Differences are deliberate:
explicit secret ownership, private path/error evidence, looped descriptor reads,
retained close failures and CA materialization from the admitted bytes.
No HTTP, worker or journal is acquired here. Settings take effect after restart.
"""

from tools.container_guard import require_container

require_container()

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import tomllib

from nmrnet_provider.input_preparation import require_preparation_settings
from nmrnet_provider.smiles import GenerationSettings
from provider_core.api_config import (
    ApiConfigurationRejected, ApiSettings, HttpsSettings,
    api_settings, https_settings,
)
from provider_core.attempt_record import RecoveryScope
from provider_core.ownership import LiveAttemptLimits
from provider_core.delivery_client import DeliveryEndpoint
from provider_core.delivery_worker import require_delivery_retry_settings
from provider_core.transport import _positive_seconds
from provider_core.chat import ChatEndpointSpec, require_chat_limits
from provider_core.conversation import require_turn_limit
from provider_core.execution import require_execution_settings, require_observation_gap
from provider_core.startup_file import StartupFileRejected, read_startup_file as core_read_startup_file
from provider_core.uploads import UploadLimits, require_store_origin

if TYPE_CHECKING:
    from nmrnet_provider.native_factory import NativePolicy
    from nmrnet_provider.service import Announcement, ServicePolicy

# The scientific offering is code-owned, as in SECS main.py:48-59. G01 uses this
# API kind for forward prediction: Magnet 600968a5fad8ad0118ea4f42cd7fc1fbfde54aaf,
# models/g_01_nmr_forward/provider/magnet_provider_bindings/g_01_nmr_forward/identity.py.
ANALYSIS_KIND_REF = "peaks_from_mol"


class ScientificRetryNotImplemented(NotImplementedError):
    """A positive scientific retry count is not supported yet."""


class ConfigurationRejected(ValueError):
    """Safe startup explanation; original file/parser evidence remains private."""

    def __init__(self, message, *, cause=None, cleanup_failures=()):
        super().__init__(f"Cannot configure NMRNet provider: {message}")
        self.cause = cause
        self.cleanup_failures = tuple(cleanup_failures)


@dataclass(frozen=True, slots=True, repr=False)
class InterpreterEndpointSettings:
    spec: ChatEndpointSpec
    http: HttpsSettings
    request_bytes: int
    response_bytes: int

    def __post_init__(self):
        require_chat_limits(request_bytes=self.request_bytes, response_bytes=self.response_bytes)


@dataclass(frozen=True, slots=True, repr=False)
class InterpreterSettings:
    endpoints: tuple[InterpreterEndpointSettings, ...]
    max_turns: int


@dataclass(frozen=True, slots=True, repr=False)
class UploadSettings:
    store_origin: str
    http: HttpsSettings
    limits: UploadLimits


@dataclass(frozen=True, slots=True, repr=False)
class PreparationSettings:
    generation_settings: GenerationSettings
    generation_seconds: float
    max_worker_calls: int
    seconds: float

    def __post_init__(self):
        require_preparation_settings(
            generation_settings=self.generation_settings,
            generation_seconds=self.generation_seconds,
            max_worker_calls=self.max_worker_calls,
            seconds=self.seconds,
        )


@dataclass(frozen=True, slots=True, repr=False)
class LifecycleSettings:
    scope: RecoveryScope
    lifetime_root: Path
    live_limits: LiveAttemptLimits
    start_replays: int
    start_seconds: float
    allowed_job_states: frozenset[str]
    poll_seconds: float
    task_settle_seconds: float
    observation_gap_seconds: float

    def __post_init__(self):
        require_observation_gap(self.observation_gap_seconds)
        _path(self.lifetime_root, "Provider lifetime directory")
        _positive_seconds(self.start_seconds)
        if type(self.start_replays) is not int or not 0 <= self.start_replays <= 1:
            raise ConfigurationRejected("lifecycle start_replays must be zero or one.")


@dataclass(frozen=True, slots=True, repr=False)
class DeliverySettings:
    endpoint: DeliveryEndpoint
    request_seconds: float
    retirement_seconds: float
    retry_initial_seconds: float
    retry_max_seconds: float

    def __post_init__(self):
        _positive_seconds(self.request_seconds)
        _positive_seconds(self.retirement_seconds)
        require_delivery_retry_settings(self.retry_initial_seconds, self.retry_max_seconds)


@dataclass(frozen=True, slots=True, repr=False)
class ServiceSettings:
    announcement: "Announcement"
    policy: "ServicePolicy"
    native: "NativePolicy"


@dataclass(frozen=True, slots=True, repr=False)
class ProviderConfig:
    api: ApiSettings
    interpreter: InterpreterSettings
    uploads: UploadSettings
    preparation: PreparationSettings
    lifecycle: LifecycleSettings
    delivery: DeliverySettings
    service: ServiceSettings
    source_sha256: str


def load_provider_config(path):
    """Read admitted startup files once; never discover credentials or endpoints."""
    return _read_provider_config(path, read_startup_file, read_api_file=read_startup_file)


def _read_provider_config(path, read_file, *, read_api_file):
    """Use the same admission path for runtime and exact-byte offline capture."""
    raw, document = _configuration_document(path, read_file)
    # Only known owner messages may cross this disclosure boundary. Raw TOML,
    # filesystem and SSL errors remain private causes instead of public text.
    try:
        api, provider_ref = _api(document["api"], read_api_file)
        return ProviderConfig(
            api,
            _interpreter(document["interpreter"], read_file),
            _uploads(document["uploads"], read_file),
            _preparation(document["preparation"]),
            _lifecycle(document["lifecycle"], api.identity.origin, provider_ref),
            _delivery(document["delivery"], api.identity.origin, provider_ref),
            _service(document["service"]),
            hashlib.sha256(raw).hexdigest(),
        )
    except ConfigurationRejected:
        raise
    except OverflowError as cause:
        raise ConfigurationRejected(
            "configured time limits must fit the runtime's finite seconds representation.", cause=cause
        ) from None
    except ValueError as cause:
        raise ConfigurationRejected(str(cause), cause=cause) from None


def _configuration_document(path, read_file):
    """Decode the one existing configuration envelope for every explicit purpose."""
    raw = read_file(path, maximum_bytes=65536, label="configuration")
    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as cause:
        raise ConfigurationRejected("configuration must be UTF-8 TOML within 65536 bytes.", cause=cause) from None
    _fields(document, {"schema_id", "api", "interpreter", "uploads", "preparation", "lifecycle", "delivery", "service", "scientific_retry"}, "configuration")
    if document["schema_id"] != "nmrnet.provider.runtime_config.v3":
        raise ConfigurationRejected("configuration requires schema nmrnet.provider.runtime_config.v3.")
    retry = document["scientific_retry"]
    _fields(retry, {"max_retries"}, "scientific_retry")
    if type(retry["max_retries"]) is not int or retry["max_retries"] < 0:
        raise ConfigurationRejected("scientific_retry.max_retries must be a nonnegative integer.")
    if retry["max_retries"] > 0:
        raise ScientificRetryNotImplemented("Cannot start NMRNet: scientific_retry.max_retries must be 0 until scientific retries are implemented.")
    return raw, document


def _fields(document, required, label, optional=frozenset()):
    if type(document) is not dict or not required <= document.keys() or document.keys() - required - optional:
        raise ConfigurationRejected(f"{label} must contain exactly its documented fields.")


def _path(value, label):
    if not isinstance(value, (str, Path)) or not str(value) or "\x00" in str(value) or not Path(value).is_absolute():
        raise ConfigurationRejected(f"{label} requires an explicit absolute container path.")
    return Path(value)


def read_startup_file(path, *, maximum_bytes, label, secret=False):
    """Keep NMRNet's startup context around the shared descriptor admission."""
    try:
        return core_read_startup_file(path, maximum_bytes=maximum_bytes, label=label, secret=secret)
    except StartupFileRejected as failure:
        raise ConfigurationRejected(
            str(failure), cause=failure.cause, cleanup_failures=failure.cleanup_failures,
        ) from None
    except BaseException as interruption:
        # Preserve the original interruption identity and earlier cause while
        # translating only the shared owner's independent close evidence.
        failure = interruption.__cause__
        if type(failure) is StartupFileRejected:
            raise interruption from ConfigurationRejected(
                str(failure), cause=failure.cause, cleanup_failures=failure.cleanup_failures,
            )
        raise


def _https(table, label, read_file):
    try:
        return https_settings(table, label, read_file)
    except ApiConfigurationRejected as failure:
        raise ConfigurationRejected(str(failure), cause=failure.cause) from None


def _api(table, read_file):
    try:
        return api_settings(table, read_file)
    except ApiConfigurationRejected as failure:
        raise ConfigurationRejected(str(failure), cause=failure.cause) from None


def _interpreter(table, read_file):
    _fields(table, {"max_turns", "endpoints"}, "interpreter")
    require_turn_limit(table["max_turns"])
    endpoints = table["endpoints"]
    if type(endpoints) is not list or not endpoints:
        raise ConfigurationRejected("interpreter requires a nonempty ordered endpoints array.")
    admitted = tuple(_endpoint(endpoint, read_file) for endpoint in endpoints)
    if len({endpoint.spec.configuration_id for endpoint in admitted}) != len(admitted):
        raise ConfigurationRejected("interpreter endpoint configuration IDs must be distinct.")
    return InterpreterSettings(admitted, table["max_turns"])


def _endpoint(table, read_file):
    _fields(
        table,
        {
            "configuration_id",
            "url",
            "model",
            "key_file",
            "request_bytes",
            "response_bytes",
            "exchange_seconds",
            "retirement_seconds",
        },
        "interpreter endpoint",
        {"ca_file", "reasoning_effort"},
    )
    raw = read_file(table["key_file"], maximum_bytes=16384, label="interpreter key", secret=True)
    try:
        # Allow one editor-added line ending, not arbitrary whitespace trimming.
        key = raw.removesuffix(b"\r\n") if raw.endswith(b"\r\n") else raw.removesuffix(b"\n")
        key = key.decode("ascii")
    except UnicodeError as cause:
        raise ConfigurationRejected(
            "interpreter key must be header-safe ASCII with at most one final LF or CRLF.", cause=cause
        ) from None
    spec = ChatEndpointSpec(
        configuration_id=table["configuration_id"],
        model=table["model"],
        url=table["url"],
        api_key=key,
        reasoning_effort=table.get("reasoning_effort"),
    )
    return InterpreterEndpointSettings(
        spec, _https(table, "interpreter", read_file), table["request_bytes"], table["response_bytes"]
    )


def _uploads(table, read_file):
    _fields(
        table,
        {"store_origin", "per_source_bytes", "total_bytes", "requests", "seconds", "exchange_seconds", "retirement_seconds"},
        "Uploads",
        {"ca_file"},
    )
    return UploadSettings(
        require_store_origin(table["store_origin"]),
        _https(table, "Uploads", read_file),
        UploadLimits(table["per_source_bytes"], table["total_bytes"], table["requests"], table["seconds"]),
    )


def _preparation(table):
    _fields(
        table, {"seed", "max_iterations", "timeout_seconds", "generation_seconds", "max_worker_calls", "seconds"}, "preparation"
    )
    return PreparationSettings(
        GenerationSettings(table["seed"], table["max_iterations"], table["timeout_seconds"]),
        table["generation_seconds"],
        table["max_worker_calls"],
        table["seconds"],
    )


def _lifecycle(table, api_origin, provider_ref):
    _fields(
        table,
        {
            "consumer_id",
            "generation_id",
            "lifetime_root",
            "maximum_live_attempts",
            "maximum_control_bytes",
            "maximum_pending_terminal_bytes",
            "start_replays",
            "start_seconds",
            "allowed_job_states",
            "poll_seconds",
            "task_settle_seconds",
            "observation_gap_seconds",
        },
        "lifecycle",
    )
    states = table["allowed_job_states"]
    if type(states) is not list or any(type(state) is not str for state in states):
        raise ConfigurationRejected("lifecycle allowed_job_states must be an explicit array of Job states.")
    states = frozenset(states)
    require_execution_settings(
        allowed_job_states=states, poll_seconds=table["poll_seconds"], task_settle_seconds=table["task_settle_seconds"]
    )
    return LifecycleSettings(
        RecoveryScope(api_origin, provider_ref, table["consumer_id"], table["generation_id"]),
        _path(table["lifetime_root"], "Provider lifetime directory"),
        LiveAttemptLimits(table["maximum_live_attempts"], table["maximum_control_bytes"],
                          table["maximum_pending_terminal_bytes"]),
        table["start_replays"],
        table["start_seconds"],
        states,
        table["poll_seconds"],
        table["task_settle_seconds"],
        table["observation_gap_seconds"],
    )


def _delivery(table, api_origin, provider_ref):
    _fields(table, {"socket_path", "socket_group_id", "request_seconds", "retirement_seconds", "retry_initial_seconds", "retry_max_seconds"}, "delivery")
    return DeliverySettings(
        DeliveryEndpoint(api_origin, provider_ref, _path(table["socket_path"], "Delivery socket"),
                         socket_group_id=table["socket_group_id"]),
        table["request_seconds"], table["retirement_seconds"],
        table["retry_initial_seconds"], table["retry_max_seconds"],
    )


def _service(table):
    # These owners depend on ProviderConfig. Decode only after module admission,
    # reusing their validators instead of mirroring policy in the file reader.
    from nmrnet_provider.admission import AdmissionPolicy  # noqa: PLC0415
    from nmrnet_provider.native_factory import NativePolicy  # noqa: PLC0415
    from nmrnet_provider.service import Announcement, ServicePolicy  # noqa: PLC0415
    from provider_core.hello import AnalysisOffering, hello_body  # noqa: PLC0415

    _fields(
        table,
        {
            "display_name",
            "description",
            "page_size",
            "maximum_pages",
            "idle_seconds",
            "api_backoff_max_seconds",
            "hello_seconds",
            "drain_seconds",
            "settle_seconds",
            "allocation_seconds",
            "prediction_seconds",
            "source_seconds",
        },
        "service",
    )
    # The installed binding owns its scientific offering; deployments supply
    # only the provider's summary and operational settings.
    try:
        offering_description = (Path(__file__).with_name("about") / "hello.md").read_text(encoding="utf-8")
    except (OSError, UnicodeError) as cause:
        raise ConfigurationRejected("cannot read the packaged offering description as UTF-8.", cause=cause) from None
    announcement = Announcement(
        table["display_name"],
        table["description"],
        (AnalysisOffering(ANALYSIS_KIND_REF, offering_description),),
    )
    hello_body(
        display_name=announcement.display_name,
        description=announcement.description,
        analysis_offerings=announcement.analysis_offerings,
    )
    return ServiceSettings(
        announcement,
        ServicePolicy(
            AdmissionPolicy(table["page_size"], table["maximum_pages"]),
            table["idle_seconds"],
            table["hello_seconds"],
            table["drain_seconds"],
            table["settle_seconds"],
            table["api_backoff_max_seconds"],
        ),
        NativePolicy(table["allocation_seconds"], table["prediction_seconds"], table["source_seconds"]),
    )
