FROM scratch AS runtime_wheels
COPY wheelhouse/runtime/ /wheels/
FROM docker.io/library/python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c

ENV NMRNET_IN_CONTAINER=1 NMR_PROVIDER_TOOLS_IN_CONTAINER=1 PROVIDER_CORE_IN_CONTAINER=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /opt/build
COPY tools/container_guard.py tools/artifacts.py tools/wheelhouse.py tools/provider_environment.py tools/baseline_prepare.py tools/container_resources.py ./tools/
COPY requirements/provider-core-install.lock ./requirements/
COPY host/bootstrap/acquisition/acquisition_tool/__init__.py ./acquisition_tool/__init__.py
COPY host/bootstrap/acquisition/acquisition_tool/installer/tools/ ./acquisition_tool/installer/tools/
COPY host/bootstrap/acquisition/acquisition_tool/installer/requirements/installer-release.json /opt/provider-tools/installer/release.json
COPY host/bootstrap/acquisition/acquisition_tool/installer/wheels/ /opt/provider-tools/installer/wheels/
COPY nmrnet_provider/ ./nmrnet_provider/
COPY nmrnet_inference/predict.py ./nmrnet_inference/predict.py
# The caller freezes both contexts and disables build networking. Only locked
# dependency wheels and the released core wheel can enter the runtime.
RUN --mount=type=bind,from=runtime_wheels,source=/wheels,target=/runtime-wheels \
    python3 -B -m acquisition_tool.installer.tools.installer --record /opt/provider-tools/installer/release.json --pool /opt/provider-tools/installer/wheels \
    && python3 -B -m tools.provider_environment --root /opt/provider --source /opt/build \
        --install-lock /opt/build/requirements/provider-core-install.lock --runtime-wheels /runtime-wheels \
    && /opt/provider/environment/bin/python -I -B -m nmrnet_provider.process --help \
    && rm -rf /opt/build
WORKDIR /opt/provider
USER 65532:65532
ENTRYPOINT ["/opt/provider/environment/bin/python", "-I", "-B", "-m", "nmrnet_provider.process"]

ARG PROVIDER_INPUTS
ARG PROVIDER_WHEELS
LABEL io.nmrnet-repro.provider-inputs="${PROVIDER_INPUTS}"
LABEL io.nmrnet-repro.provider-wheels="${PROVIDER_WHEELS}"
