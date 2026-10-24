"""Run the actual process with recording startup barriers, never native proof.

Idle receiving tests supply no runner sockets. Only prove_previous_retired is
replaced here; signing, checkpoint/relay ownership, signals and diagnostics use
the production process. Real serial-runner barriers have separate deployment proof.
"""

from unittest.mock import patch

from nmrnet_provider.native_factory import NativeAttemptFactory
from nmrnet_provider.process import main


async def record_startup_barriers(factory):
    if factory.allocations:
        raise AssertionError("Receiving fixture startup unexpectedly owns native allocations")
    root = factory.config.lifecycle.store_root.parent / "diagnostics"
    (root / "recording-startup-native-barriers").write_bytes(b"recording only; no native operation dispatched\n")


if __name__ == "__main__":
    with patch.object(NativeAttemptFactory, "prove_previous_retired", record_startup_barriers):
        raise SystemExit(main())
