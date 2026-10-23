FROM docker.io/library/python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c

ARG NMRNET_REVISION
ARG INFERENCE_INPUTS
LABEL io.nmrnet-repro.upstream-revision="${NMRNET_REVISION}"
LABEL io.nmrnet-repro.inference-inputs="${INFERENCE_INPUTS}"
# Build-time repository tools use the same admission marker as contained runtime lanes.
ENV NMRNET_IN_CONTAINER=1 \
    PROVIDER_CORE_IN_CONTAINER=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    OMP_NUM_THREADS=2 \
    MKL_NUM_THREADS=2

WORKDIR /opt/build
COPY tools/container_guard.py tools/artifacts.py tools/wheelhouse.py tools/build_unicore.py ./tools/
COPY requirements/inference.lock requirements/unicore-source.json /opt/inference/
RUN --mount=type=bind,from=wheelhouse,target=/wheelhouse \
    python3 -m pip install --no-index --require-hashes --only-binary=:all: \
        --find-links /wheelhouse --no-cache-dir --disable-pip-version-check \
        -r /opt/inference/inference.lock \
    && python3 -B -m tools.build_unicore \
        /wheelhouse/unicore-source.tar.gz /opt/inference/unicore-source.json \
    && python3 -m pip check

COPY tools/provider_environment.py tools/provider_proxy.py tools/bound_http_proxy.py tools/baseline_prepare.py tools/container_resources.py ./tools/
COPY requirements/provider-core.lock requirements/provider-core-build.lock ./requirements/
COPY packages/provider-core/ ./packages/provider-core/
COPY nmrnet_provider/ ./nmrnet_provider/
COPY nmrnet_inference/ ./nmrnet_inference/
RUN --mount=type=bind,from=runtime-wheels,target=/runtime-wheels \
    --mount=type=bind,from=build-wheels,target=/build-wheels \
    python3 -B -m tools.provider_environment --root /opt/inference --source /opt/build \
        --python /usr/local/bin/python --runtime-wheels /runtime-wheels --build-wheels /build-wheels \
    && python3 -I -B -m nmrnet_provider.worker_main --help \
    && rm -rf /opt/build

# -I children consume image-owned installed upstream code, without PYTHONPATH.
COPY NMRNetplusplus/uninmr/ /usr/local/lib/python3.12/site-packages/uninmr/
COPY NMRNetplusplus/LICENSE /opt/nmrnet/LICENSE
WORKDIR /opt/nmrnet
USER 65532:65532
CMD ["python3", "-B", "-c", "import torch, unicore, uninmr, uninmr.infer; assert torch.version.cuda is None; assert torch.get_default_dtype() == torch.float32; print('Inference imports passed: CPU float32; torch=' + torch.__version__)"]
