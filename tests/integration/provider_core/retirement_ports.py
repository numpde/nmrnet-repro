"""Retire recording ports whose fake channel and native owner are independent.

Production NativeOperation observes its own transport. These recording fixtures
have separate close/retirement observations so tests can inject either failure.
"""

import asyncio

async def retire_worker_channel(worker, native_retirement):
    """Observe local closure and confirmed child retirement and scratch release, retaining every failure.

    Prediction and source-reader sessions own their operation states. This
    shared sequence never declares them retired and never replaces an owner.
    Cancellation is returned with other failures for that owner to re-raise.
    """
    failures = []
    try:
        worker.close()
    except Exception as cause:
        failures.append(("channel_close", cause))
    try:
        await native_retirement.retire()
    except (Exception, asyncio.CancelledError) as cause:
        failures.append(("native_retirement", cause))
    try:
        await worker.wait_closed()
    except (Exception, asyncio.CancelledError) as cause:
        failures.append(("channel_observation", cause))
    return failures
