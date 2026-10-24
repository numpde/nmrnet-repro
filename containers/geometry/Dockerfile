FROM docker.io/library/python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c

ARG GEOMETRY_INPUTS
LABEL io.nmrnet-repro.geometry-inputs="${GEOMETRY_INPUTS}"
ENV NMRNET_IN_CONTAINER=1 PROVIDER_CORE_IN_CONTAINER=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /opt/build
COPY tools/container_guard.py tools/artifacts.py tools/wheelhouse.py tools/rdkit_environment.py ./tools/
COPY requirements/preparation.lock /opt/rdkit/preparation.lock
# Hash verification and installation consume the same frozen, read-only wheels.
# Build networking is disabled by the launcher; no source distributions execute.
COPY tools/provider_environment.py tools/core_environment.py tools/provider_proxy.py tools/bound_http_proxy.py tools/baseline_prepare.py tools/container_resources.py ./tools/
COPY requirements/provider-core.lock requirements/provider-core-build.lock ./requirements/
COPY packages/provider-core/ ./packages/provider-core/
COPY nmrnet_provider/ ./nmrnet_provider/
COPY nmrnet_inference/ ./nmrnet_inference/
RUN --mount=type=bind,from=wheelhouse,target=/wheels \
    --mount=type=bind,from=runtime-wheels,target=/runtime-wheels \
    --mount=type=bind,from=build-wheels,target=/build-wheels \
    python3 -B -m tools.rdkit_environment --root /opt/rdkit \
        --wheels /wheels --lock /opt/rdkit/preparation.lock \
    && python3 -B -m tools.provider_environment --root /opt/rdkit --source /opt/build \
        --python /opt/rdkit/environment/bin/python --runtime-wheels /runtime-wheels --build-wheels /build-wheels \
    && /opt/rdkit/environment/bin/python -I -B -m nmrnet_provider.worker_main --help \
    && rm -rf /opt/build

WORKDIR /opt/rdkit
USER 65532:65532
CMD ["/opt/rdkit/environment/bin/python", "-I", "-B", "-c", "import rdkit, numpy, PIL"]
