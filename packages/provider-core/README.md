# provider-core

Local, independently installed NMR API provider infrastructure. Implementation
proceeds in the slices recorded in ../../notes/017_provider_core_plan_20261018.txt.
Scientific interpretation and execution remain consumer-owned.

The supported test targets run offline in containers. The runtime launcher must
set PROVIDER_CORE_IN_CONTAINER=1; this accidental-host-execution guard does not
attest to isolation. The host/container composition owns actual grants.

Source contracts: nmr-api-v1 at 6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8,
nmr_api/provider/schemas/job_input_read_response.v1.schema.json and
nmr_api/job_specification.py; Magnet at c5813ee35cfbb1c98a5cc5d20a94787271f26486,
magnet_provider/job_input.py. Exact Job bytes are preserved before science-specific
admission. The verifier binds caller-supplied observations; it does not authenticate
a network peer or grant authority.
