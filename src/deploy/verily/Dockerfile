# Build the source/ context produced by deploy/verily/prepare_release.py.
# The stamped source distribution preserves Git identity inside the image.
FROM python:3.12-slim-bookworm AS pipeline
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PATHND_DATA_DIR=/opt/pathnd-models \
    OMP_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1 \
    MKL_NUM_THREADS=1
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates git libgomp1 libopenslide0 && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/pathnd-src
COPY pyproject.toml MANIFEST.in README.md USAGE_CLI.md USAGE_LIBRARY.md build_hooks.py package-freeze.json ./
COPY pathnd_qc ./pathnd_qc
COPY deploy/verily/prepare_release.py ./deploy/verily/prepare_release.py
RUN python deploy/verily/prepare_release.py --check
RUN python -m pip install 'torch>=2.6,<3' torchvision --index-url https://download.pytorch.org/whl/cpu \
    && python -m pip install . \
    && pathnd-qc setup grandqc \
    && python -m pip check \
    && python -c "import pathnd_qc; assert pathnd_qc.__version__ == '0.5.0'" \
    && python -c "from pathnd_qc.qc_slide.folds import detect_folds, fline_feature, fline_threshold; from pathnd_qc.config import cfg; assert cfg('m2.folds.fline_enabled') is True" \
    && python -m pip freeze > /opt/pathnd-requirements.txt
LABEL org.opencontainers.image.title="Path-ND QC" \
      org.opencontainers.image.version="0.5.0"
COPY deploy/verily/run_workflow.py /opt/pathnd/run_workflow.py
WORKDIR /work
CMD ["pathnd-qc", "--help"]

# All inference dependencies and GrandQC checkpoints are installed at build time.
# Supply pen weights as a workflow File input or mount a registered model directory.
