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
import os
import ssl
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import tomllib

from nmrnet_provider.input_preparation import require_preparation_settings
from nmrnet_provider.smiles import GenerationSettings
from provider_core.api import require_api_topology
from provider_core.attempt_files import StoreLimits
from provider_core.attempt_record import MAX_RECORD_BYTES, RecoveryScope
from provider_core.attempt_store import require_store_settings
from provider_core.chat import ChatEndpointSpec, require_chat_limits
from provider_core.conversation import require_turn_limit
from provider_core.credential import MAX_CREDENTIAL_BYTES, parse_provider_credential
from provider_core.execution import require_execution_settings
from provider_core.signing import SigningIdentity
from provider_core.transport import HttpTransport, require_transport_settings
from provider_core.uploads import UploadLimits, require_store_origin

if TYPE_CHECKING:
    from nmrnet_provider.native_factory import NativePolicy
    from nmrnet_provider.service import Announcement, ServicePolicy

# The scientific offering is code-owned, as in SECS main.py:48-59. G01 uses this
# API kind for forward prediction: Magnet 600968a5fad8ad0118ea4f42cd7fc1fbfde54aaf,
# models/g_01_nmr_forward/provider/magnet_provider_bindings/g_01_nmr_forward/identity.py.
ANALYSIS_KIND_REF = "peaks_from_mol"


class ConfigurationRejected(ValueError):
    """Safe startup explanation; original file/parser evidence remains private."""

    def __init__(self, message, *, cause=None, cleanup_failures=()):
        super().__init__(f"Cannot configure NMRNet provider: {message}")
        self.cause = cause
        self.cleanup_failures = tuple(cleanup_failures)


@dataclass(frozen=True, slots=True, repr=False)
class HttpsSettings:
    trust: ssl.SSLContext
    exchange_seconds: float
    retirement_seconds: float
    proxy_socket: str | None = None

    def __post_init__(self):
        require_transport_settings(
            trust=self.trust,
            exchange_seconds=self.exchange_seconds,
            retirement_seconds=self.retirement_seconds,
            proxy_socket=self.proxy_socket,
        )

    def new_transport(self):
        """Give each runtime role a fresh, independently retired exchange owner."""
        return HttpTransport(
            trust=self.trust,
            exchange_seconds=self.exchange_seconds,
            retirement_seconds=self.retirement_seconds,
            proxy_socket=self.proxy_socket,
        )


@dataclass(frozen=True, slots=True, repr=False)
class ApiSettings:
    identity: SigningIdentity
    http: HttpsSettings
    expected_topology: str

    def __post_init__(self):
        require_api_topology(self.expected_topology)


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
    store_root: Path
    store_limits: StoreLimits
    store_seconds: float
    allowed_job_states: frozenset[str]
    poll_seconds: float
    task_settle_seconds: float

    def __post_init__(self):
        require_store_settings(
            root=self.store_root, scope=self.scope, limits=self.store_limits, operation_seconds=self.store_seconds
        )


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
    service: ServiceSettings
    source_sha256: str


def load_provider_config(path):
    """Read admitted startup files once; never discover credentials or endpoints."""
    raw = read_startup_file(path, maximum_bytes=65536, label="configuration")
    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as cause:
        raise ConfigurationRejected("configuration must be UTF-8 TOML within 65536 bytes.", cause=cause) from None
    _fields(document, {"schema_id", "api", "interpreter", "uploads", "preparation", "lifecycle", "service"}, "configuration")
    if document["schema_id"] != "nmrnet.provider.runtime_config.v1":
        raise ConfigurationRejected("configuration requires schema nmrnet.provider.runtime_config.v1.")
    # Only known owner messages may cross this disclosure boundary. Raw TOML,
    # filesystem and SSL errors remain private causes instead of public text.
    try:
        api, provider_ref = _api(document["api"])
        return ProviderConfig(
            api,
            _interpreter(document["interpreter"]),
            _uploads(document["uploads"]),
            _preparation(document["preparation"]),
            _lifecycle(document["lifecycle"], api.identity.origin, provider_ref),
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


def _fields(document, required, label, optional=frozenset()):
    if type(document) is not dict or not required <= document.keys() or document.keys() - required - optional:
        raise ConfigurationRejected(f"{label} must contain exactly its documented fields.")


def _path(value, label):
    if not isinstance(value, (str, Path)) or not str(value) or "\x00" in str(value) or not Path(value).is_absolute():
        raise ConfigurationRejected(f"{label} requires an explicit absolute container path.")
    return Path(value)


def read_startup_file(path, *, maximum_bytes, label, secret=False):
    """Read the admitted descriptor; trusted parent mounts remain host-owned.

    O_NOFOLLOW protects the final component only. Read and close failures remain
    separately inspectable, following provider_core.attempt_files' ownership.
    """
    path = _path(path, label)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as cause:
        raise ConfigurationRejected(
            f"{label} could not be opened as a regular file without a final symlink.", cause=cause
        ) from None
    content, primary, cleanup = None, None, None
    try:
        status = os.fstat(descriptor)
        mode = stat.S_IMODE(status.st_mode)
        if not stat.S_ISREG(status.st_mode) or status.st_size > maximum_bytes:
            raise ConfigurationRejected(f"{label} must be a regular file within {maximum_bytes} bytes.")
        if secret:
            if status.st_uid != os.geteuid() or mode not in (0o400, 0o600):
                raise ConfigurationRejected(f"{label} must belong to the container user with mode 0400 or 0600.")
        elif status.st_uid not in (0, os.geteuid()) or mode & 0o022:
            raise ConfigurationRejected(f"{label} must belong to root or the container user and forbid group/other writes.")
        content = _read_contents(descriptor, status.st_size, maximum_bytes, label)
    except BaseException as cause:
        primary = cause
    try:
        os.close(descriptor)
    except OSError as cause:
        cleanup = cause
    if primary is not None and not isinstance(primary, Exception):
        if cleanup is not None:
            raise primary from ConfigurationRejected(
                f"{label} close also failed during interruption.", cause=primary.__cause__, cleanup_failures=(cleanup,)
            )
        raise primary
    if primary is not None or cleanup is not None:
        if isinstance(primary, ConfigurationRejected) and cleanup is None:
            raise primary
        raise ConfigurationRejected(
            f"{label} could not be read and closed; no runtime resources were acquired.",
            cause=primary,
            cleanup_failures=() if cleanup is None else (cleanup,),
        ) from None
    return content


def _read_contents(descriptor, expected_bytes, maximum_bytes, label):
    content = bytearray()
    while len(content) <= maximum_bytes:
        part = os.read(descriptor, maximum_bytes + 1 - len(content))
        if not part:
            break
        content.extend(part)
    if len(content) != expected_bytes or len(content) > maximum_bytes:
        raise ConfigurationRejected(f"{label} changed length during its bounded startup read.")
    return bytes(content)


def _https(table, label):
    # CPython v3.12.12 Lib/ssl.py::create_default_context honors SSLKEYLOGFILE;
    # OpenSSL default roots honor SSL_CERT_FILE/DIR. Neither is a startup grant.
    if any(name in os.environ for name in ("SSL_CERT_FILE", "SSL_CERT_DIR", "SSLKEYLOGFILE")):
        raise ConfigurationRejected(
            "remove ambient SSL_CERT_FILE, SSL_CERT_DIR and SSLKEYLOGFILE; configure each explicit ca_file instead."
        )
    if "ca_file" in table:
        raw = read_startup_file(table["ca_file"], maximum_bytes=1048576, label=label + " CA")
        try:
            # cadata replaces system roots and consumes these bytes, never a
            # second pathname lookup after the descriptor has been admitted.
            trust = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            trust.load_verify_locations(cadata=raw.decode("ascii"))
        except (UnicodeError, ValueError, OSError) as cause:
            raise ConfigurationRejected(f"{label} CA must contain PEM certificates within 1048576 bytes.", cause=cause) from None
    else:
        try:
            trust = ssl.create_default_context()
        except (OSError, ssl.SSLError) as cause:
            raise ConfigurationRejected(f"{label} system TLS roots could not be loaded.", cause=cause) from None
    return HttpsSettings(trust, table["exchange_seconds"], table["retirement_seconds"])


def _api(table):
    _fields(
        table,
        {"origin", "profile", "topology", "provider_ref", "credential_file", "exchange_seconds", "retirement_seconds"},
        "API",
        {"ca_file"},
    )
    credential = parse_provider_credential(
        read_startup_file(
            table["credential_file"], maximum_bytes=MAX_CREDENTIAL_BYTES, label="API signing credential", secret=True
        )
    )
    if (credential.profile, credential.provider_ref) != (table["profile"], table["provider_ref"]):
        raise ConfigurationRejected("API signing credential does not match the configured profile and provider.")
    identity = SigningIdentity(table["origin"], credential.credential_ref, credential.private_key)
    return ApiSettings(identity, _https(table, "API"), table["topology"]), credential.provider_ref


def _interpreter(table):
    _fields(table, {"max_turns", "endpoints"}, "interpreter")
    require_turn_limit(table["max_turns"])
    endpoints = table["endpoints"]
    if type(endpoints) is not list or not endpoints:
        raise ConfigurationRejected("interpreter requires a nonempty ordered endpoints array.")
    admitted = tuple(_endpoint(endpoint) for endpoint in endpoints)
    if len({endpoint.spec.configuration_id for endpoint in admitted}) != len(admitted):
        raise ConfigurationRejected("interpreter endpoint configuration IDs must be distinct.")
    return InterpreterSettings(admitted, table["max_turns"])


def _endpoint(table):
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
    raw = read_startup_file(table["key_file"], maximum_bytes=16384, label="interpreter key", secret=True)
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
    return InterpreterEndpointSettings(spec, _https(table, "interpreter"), table["request_bytes"], table["response_bytes"])


def _uploads(table):
    _fields(
        table,
        {"store_origin", "per_source_bytes", "total_bytes", "requests", "seconds", "exchange_seconds", "retirement_seconds"},
        "Uploads",
        {"ca_file"},
    )
    return UploadSettings(
        require_store_origin(table["store_origin"]),
        _https(table, "Uploads"),
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
            "store_root",
            "maximum_records",
            "filesystem_reserve_bytes",
            "store_seconds",
            "allowed_job_states",
            "poll_seconds",
            "task_settle_seconds",
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
        _path(table["store_root"], "Attempt store"),
        StoreLimits(table["maximum_records"], MAX_RECORD_BYTES, table["filesystem_reserve_bytes"]),
        table["store_seconds"],
        states,
        table["poll_seconds"],
        table["task_settle_seconds"],
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
            "offering_description",
            "page_size",
            "maximum_pages",
            "idle_seconds",
            "hello_seconds",
            "drain_seconds",
            "settle_seconds",
            "allocation_seconds",
            "prediction_seconds",
            "source_seconds",
        },
        "service",
    )
    announcement = Announcement(
        table["display_name"],
        table["description"],
        (AnalysisOffering(ANALYSIS_KIND_REF, table["offering_description"]),),
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
        ),
        NativePolicy(table["allocation_seconds"], table["prediction_seconds"], table["source_seconds"]),
    )
