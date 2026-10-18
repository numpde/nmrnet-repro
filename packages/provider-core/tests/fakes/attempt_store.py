"""Synthetic pending starts for contained storage ownership tests."""

from provider_core.attempt_record import RecoveryScope, StartPending
from tests.fakes.attempts import retained_start
from tests.fakes.job_input import facts


def scope():
    return RecoveryScope("https://api.example", "provider:example", "lane:test", "generation:one")


def record(key="retained:one"):
    return StartPending(scope(), "nmr_predict", facts(), retained_start(provider_attempt_key=key))
