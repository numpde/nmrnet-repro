"""Small exact terminal requests shared by delivery queue checks."""

from provider_core.attempt_record import TerminalOperation
from provider_core.attempt_request import fail_body
from provider_core.delivery_store import DeliveryOutcome
from tests.fakes.attempts import ATTEMPT_REF


def outcome(*, provider="provider:test", suffix="0", message="SCIENTIFIC_PAYLOAD_NEEDLE"):
    reference = ATTEMPT_REF[:-1] + suffix
    return DeliveryOutcome(
        "https://api.example", provider, "job:queue", reference,
        TerminalOperation.FAIL, fail_body(reference, "provider_interrupted", message) + b"\n",
    )
