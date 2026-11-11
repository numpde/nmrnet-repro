# Use the selected local engine's built-in Dockerfile frontend. An external
# syntax directive would add a registry fetch outside the acquisition owner;
# --network=none and --pull=false do not constrain that independent fetch.
# Existing qualified inference builds use this same local-frontend boundary:
# https://github.com/numpde/nmrnet-repro/blob/ad7048cbb8f0dd1b4d273bfdf47ac58568d34f74/containers/inference/Dockerfile
# Dependency acquisition is a separate explicit lane. This build has no network.
FROM scratch AS checker_wheels
COPY checker-wheels/ /wheelhouse/
FROM scratch AS checker_dependencies
COPY checker-dependencies/ /dependencies/
FROM docker.io/library/python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c

ENV NMRNET_IN_CONTAINER=1 NMR_PROVIDER_TOOLS_IN_CONTAINER=1 PYTHONDONTWRITEBYTECODE=1
COPY host/bootstrap/acquisition/acquisition_tool/installer/tools/ /usr/local/lib/python3.12/site-packages/acquisition_tool/installer/tools/
COPY host/bootstrap/acquisition/acquisition_tool/installer/requirements/installer-release.json /opt/provider-tools/installer/release.json
COPY host/bootstrap/acquisition/acquisition_tool/installer/wheels/ /opt/provider-tools/installer/wheels/
RUN python3 -B -m acquisition_tool.installer.tools.installer \
    --record /opt/provider-tools/installer/release.json --pool /opt/provider-tools/installer/wheels
WORKDIR /opt/checker
COPY tools/checker.py tools/artifacts.py tools/container_guard.py ./tools/
COPY requirements/checker-dependencies.json ./requirements/checker-dependencies.json
RUN --mount=type=bind,from=checker_dependencies,source=/dependencies,target=/dependencies,ro \
    --mount=type=bind,from=checker_wheels,source=/wheelhouse,target=/wheelhouse,ro \
    python3 -B -m tools.checker install \
        --declaration /opt/checker/requirements/checker-dependencies.json \
        --directory /dependencies --wheelhouse /wheelhouse \
        --base docker.io/library/python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c

# The runtime launcher supplies the caller's nonzero UID/GID explicitly.
USER 65532:65532
WORKDIR /workspace
