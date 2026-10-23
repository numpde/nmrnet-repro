FROM docker.io/library/python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c

ARG PROVIDER_INPUTS
LABEL io.nmrnet-repro.provider-inputs="${PROVIDER_INPUTS}"
ENV NMRNET_IN_CONTAINER=1 PROVIDER_CORE_IN_CONTAINER=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /opt/build
COPY tools/container_guard.py tools/artifacts.py tools/wheelhouse.py tools/provider_environment.py tools/provider_proxy.py tools/bound_http_proxy.py tools/baseline_prepare.py tools/container_resources.py ./tools/
COPY requirements/provider-core.lock requirements/provider-core-build.lock ./requirements/
COPY packages/provider-core/ ./packages/provider-core/
COPY nmrnet_provider/ ./nmrnet_provider/
COPY nmrnet_inference/predict.py ./nmrnet_inference/predict.py
# The caller freezes both contexts and disables build networking. Only locked
# dependency wheels and the core wheel built here can enter the runtime.
RUN --mount=type=bind,from=runtime-wheels,target=/runtime-wheels \
    --mount=type=bind,from=build-wheels,target=/build-wheels \
    python3 -B -m tools.provider_environment --root /opt/provider --source /opt/build \
        --runtime-wheels /runtime-wheels --build-wheels /build-wheels \
    && /opt/provider/environment/bin/python -I -B -m nmrnet_provider.process --help \
    && rm -rf /opt/build
WORKDIR /opt/provider
USER 65532:65532
ENTRYPOINT ["/opt/provider/environment/bin/python", "-I", "-B", "-m", "nmrnet_provider.process"]
