FROM docker.io/library/python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c

ARG DELIVERY_INPUTS
LABEL io.nmrnet-repro.delivery-inputs="${DELIVERY_INPUTS}"
ENV NMRNET_IN_CONTAINER=1 PROVIDER_CORE_IN_CONTAINER=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /opt/build
COPY tools/container_guard.py tools/artifacts.py tools/wheelhouse.py tools/core_environment.py ./tools/
COPY requirements/provider-core.lock requirements/provider-core-build.lock ./requirements/
COPY packages/provider-core/ ./packages/provider-core/
COPY containers/delivery/check_runtime.py ./check_runtime.py
RUN --mount=type=bind,from=runtime-wheels,target=/runtime-wheels \
    --mount=type=bind,from=build-wheels,target=/build-wheels \
    python3 -B -m tools.core_environment --root /opt/provider-core --source /opt/build \
        --runtime-wheels /runtime-wheels --build-wheels /build-wheels \
    && /opt/provider-core/environment/bin/python -I -B -m provider_core.delivery_main --help
# The pinned Python minor fixes this venv path. Both roles use the installed
# core interpreter: the proxy also needs core's process-readiness owner.
COPY tools/container_guard.py tools/provider_proxy.py tools/bound_http_proxy.py /opt/provider-core/environment/lib/python3.12/site-packages/tools/
RUN /opt/provider-core/environment/bin/python -I -B /opt/build/check_runtime.py \
    && /opt/provider-core/environment/bin/python -I -B -m tools.provider_proxy --help \
    && rm -rf /opt/build
WORKDIR /opt/provider-core
USER 65532:65532
ENTRYPOINT ["/opt/provider-core/environment/bin/python", "-I", "-B", "-m", "provider_core.delivery_main"]
