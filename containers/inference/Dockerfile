FROM docker.io/library/python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c

ARG NMRNET_REVISION
LABEL io.nmrnet-repro.upstream-revision="${NMRNET_REVISION}"
# Build-time repository tools use the same admission marker as contained runtime lanes.
ENV NMRNET_IN_CONTAINER=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/opt/nmrnet \
    OMP_NUM_THREADS=2 \
    MKL_NUM_THREADS=2

WORKDIR /opt/build
COPY tools/container_guard.py tools/artifacts.py tools/wheelhouse.py tools/build_unicore.py ./tools/
COPY requirements/inference.lock requirements/unicore-source.json /opt/inference/
RUN --mount=type=bind,from=wheelhouse,target=/wheelhouse \
    python3 -B -m tools.wheelhouse check --profile inference \
        --lock /opt/inference/inference.lock \
        --source /opt/inference/unicore-source.json --output /wheelhouse \
    && python3 -m pip install --no-index --require-hashes --only-binary=:all: \
        --find-links /wheelhouse --no-cache-dir --disable-pip-version-check \
        -r /opt/inference/inference.lock \
    && python3 -B -m tools.build_unicore \
        /wheelhouse/unicore-source.tar.gz /opt/inference/unicore-source.json \
    && python3 -m pip check \
    && rm -rf /opt/build

COPY NMRNetplusplus/uninmr/ /opt/nmrnet/uninmr/
COPY NMRNetplusplus/LICENSE /opt/nmrnet/LICENSE
WORKDIR /opt/nmrnet
USER 65532:65532
CMD ["python3", "-B", "-c", "import torch, unicore, uninmr, uninmr.infer; assert torch.version.cuda is None; assert torch.get_default_dtype() == torch.float32; print('Inference imports passed: CPU float32; torch=' + torch.__version__)"]
