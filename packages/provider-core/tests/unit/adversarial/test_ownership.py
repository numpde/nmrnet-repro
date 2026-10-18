"""Independent lanes cannot concurrently own the same remote Attempt."""

import unittest
from dataclasses import replace

from provider_core.ownership import AttemptClaims, OwnershipRefused
from tests.fakes.attempt_store import scope
from tests.fakes.attempts import ATTEMPT_REF


class AttemptOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_lane_generation_differences_do_not_partition_remote_attempt_ownership(self):
        claims = AttemptClaims()
        first = claims.claim(scope(), ATTEMPT_REF)
        with self.assertRaises(OwnershipRefused):
            claims.claim(replace(scope(), consumer_id="another_lane", generation_id="another_generation"), ATTEMPT_REF)
        owner = object()
        first.begin(owner)
        with self.assertRaises(OwnershipRefused):
            first.release()
        with self.assertRaises(OwnershipRefused):
            first.finish(object())
        first.finish(owner)
        first.release()
        second = claims.claim(scope(), ATTEMPT_REF)
        with self.assertRaises(OwnershipRefused):
            first.release()
        self.assertTrue(second.active)
        second.release()
