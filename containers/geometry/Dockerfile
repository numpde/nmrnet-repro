FROM docker.io/library/python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c

ARG GEOMETRY_INPUTS
LABEL io.nmrnet-repro.geometry-inputs="${GEOMETRY_INPUTS}"
ENV NMRNET_IN_CONTAINER=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /opt/build
COPY tools/container_guard.py tools/artifacts.py tools/wheelhouse.py tools/rdkit_environment.py ./tools/
COPY requirements/preparation.lock /opt/rdkit/preparation.lock
# Hash verification and installation consume the same frozen, read-only wheels.
# Build networking is disabled by the launcher; no source distributions execute.
RUN --mount=type=bind,from=wheelhouse,target=/wheels \
    python3 -B -m tools.rdkit_environment --root /opt/rdkit \
        --wheels /wheels --lock /opt/rdkit/preparation.lock \
    && rm -rf /opt/build

WORKDIR /opt/rdkit
USER 65532:65532
CMD ["/opt/rdkit/environment/bin/python", "-I", "-B", "-c", "import rdkit, numpy, PIL"]
