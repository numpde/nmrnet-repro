Retained scientific input fixtures; no new predictions or expected outputs.

references.xyz and references.manifest.json are byte-identical to the completed
reference input producer retained by the original service qualification:
  producer revision c294dca413cc8f4a9309533c1e4a5764d5baa763
  https://github.com/numpde/nmrnet-repro/blob/c294dca413cc8f4a9309533c1e4a5764d5baa763/tests/integration/provider_worker/reference_fixture.py

capacity.xyz and capacity.manifest.json preserve the completed capacity producer:
  producer revision 7e5d3dad6b14d76bd3b265d39f3e6863bfbae007
  https://github.com/numpde/nmrnet-repro/blob/7e5d3dad6b14d76bd3b265d39f3e6863bfbae007/tests/integration/provider_worker/reference_fixture.py

Both historical producers used image:
  sha256:cabb4fe3e36541b0fefe54ca7c16fb21f9a60112a1e3d47992d4aa18343ecd08
and upstream revision:
  3ba8b9c2f3d0a390d00685d238618345c6ab24d7
Those identities describe the historical export, never the current worker.

Pinning lives in tests/integration/provider_core/scientific_inputs.py. Its loader
reuses reference_fixtures.verify_fixture and require_reference_inputs against
artifacts/reference-cpu-float32.json and artifacts/prediction-cpu-float32.json.
The original manifests retain raw record/request identities and atom positions.
Current predictions must be compared to the original packets; never regenerate
expectations from a new execution.

Capacity is the existing synthetic H128/C128 recipe, not plausible chemistry or
an accuracy oracle. capacity_service.require_capacity_inputs owns its exact
coordinate recipe; output qualification checks all 128 indices of each nucleus.

Retaining these small exports makes qualification portable without depending on
an untracked local .work tree or rerunning a historical producer image. Original
producer metadata is preserved verbatim for scientific reproducibility.
