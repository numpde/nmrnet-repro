"""Exclude overlapping owners of a remote Attempt within one event loop.

Precedent: magnet-deploy c5813ee35cfbb1c98a5cc5d20a94787271f26486,
magnet_provider/provider_attempt_recovery.py::ProviderAttemptOwnership. Composition shares one
registry across lanes/recovery. Unlike automatic context-manager release, an
unfinished operation pins its exact handle until its owner confirms settlement.
This is local exclusion, not a distributed lease or proof of worker retirement.
"""

from provider_core.container_guard import require_container

require_container()

import asyncio

from provider_core.attempt_record import RecoveryScope
from provider_core.attempt_response import require_attempt_ref


class OwnershipRefused(RuntimeError):
    """No claim or operation was acquired; existing ownership remains unchanged."""


def _identity(scope, reference):
    if type(scope) is not RecoveryScope:
        raise ValueError("Attempt ownership requires an admitted recovery scope")
    require_attempt_ref(reference)
    # Lane/generation labels cannot partition ownership of one remote Attempt.
    return scope.api_origin, scope.provider_ref, reference


class AttemptClaims:
    def __init__(self):
        self._claims = {}
        self._loop = None

    def claim(self, scope, execution_attempt_ref):
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            raise OwnershipRefused("Attempt claims must remain on their owning event loop")
        self._loop = loop
        identity = _identity(scope, execution_attempt_ref)
        if identity in self._claims:
            raise OwnershipRefused("This Attempt already has a local owner; do not publish or recover it concurrently")
        claim = _AttemptClaim(self, identity)
        self._claims[identity] = claim
        return claim

    def _require(self, claim):
        if asyncio.get_running_loop() is not self._loop or self._claims.get(claim._identity) is not claim:
            raise OwnershipRefused("Attempt claim is released, stale or belongs to another event loop")


class _AttemptClaim:
    def __init__(self, registry, identity):
        self._registry = registry
        self._identity = identity
        self._operation_owner = None

    @property
    def active(self):
        return self._registry._claims.get(self._identity) is self

    def require(self, scope, execution_attempt_ref):
        self._registry._require(self)
        if self._identity != _identity(scope, execution_attempt_ref):
            raise OwnershipRefused("Attempt claim differs from the API, provider or Attempt being used")

    def begin(self, owner):
        self._registry._require(self)
        if owner is None:
            raise ValueError("Attempt operation requires an explicit owner")
        if self._operation_owner is not None:
            raise OwnershipRefused("Attempt claim still owns an operation; settle it before another effect")
        self._operation_owner = owner

    def finish(self, owner):
        self._registry._require(self)
        if self._operation_owner is not owner:
            raise OwnershipRefused("Only the exact operation owner can settle this Attempt claim")
        self._operation_owner = None

    def release(self):
        self._registry._require(self)
        if self._operation_owner is not None:
            raise OwnershipRefused("Cannot release Attempt claim while an operation remains unresolved")
        del self._registry._claims[self._identity]
