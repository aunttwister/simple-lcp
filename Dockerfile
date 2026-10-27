# ── Runtime image ───────────────────────────────────────────────────────────
FROM python:3.11-slim

WORKDIR /app

# ── git — required by the in-UI runtime install of LiveBench ────────────────
# (the "benchmark plugin" clones its checkout at runtime when not baked in)
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ── Dependencies (layer-cached: only reinstalls when pyproject.toml changes) ──
COPY pyproject.toml .
COPY src/__init__.py ./src/__init__.py
RUN pip install --no-cache-dir .

# ── Pre-download tiktoken BPE vocabulary so it never hits the CDN at runtime ──
ENV TIKTOKEN_CACHE_DIR=/app/data/tiktoken_cache
RUN mkdir -p /app/data/tiktoken_cache && \
    python3 -c "import tiktoken; tiktoken.get_encoding('cl100k_base')"

# ── LiveBench (OPTIONAL — benchmark "plugin") ────────────────────────────────
# The in-UI benchmark runner is opt-in. Set the build arg WITH_BENCH=1 to bake
# a LiveBench checkout into the image; the default (WITH_BENCH=0) keeps the
# gateway image lean and the benchmark runner reports "unavailable".
#
#   docker compose build --build-arg WITH_BENCH=1 lcp
#
# Full LiveBench (core + code_runner/requirements_eval.txt) covers all 6
# categories including `coding` (which executes generated code and needs
# TensorFlow + the scientific stack). Core-only covers the other 5.
#
# Checkouts live under LCP_MODULES_DIR (default /opt/lcp-modules). The runtime
# installer clones to $LCP_MODULES_DIR/livebench; set LCP_MODULES_DIR to a
# Docker volume mount so installs survive container recreation.
ARG WITH_BENCH=0
ENV LCP_MODULES_DIR=/opt/lcp-modules
RUN if [ "$WITH_BENCH" = "1" ]; then \
        mkdir -p "${LCP_MODULES_DIR}" \
        && git clone --depth 1 https://github.com/LiveBench/LiveBench.git "${LCP_MODULES_DIR}/livebench" \
        && cd "${LCP_MODULES_DIR}/livebench" \
        && pip install --no-cache-dir -e . \
        && pip install --no-cache-dir -r code_runner/requirements_eval.txt ; \
    fi

# ── Semantic routing + memory (OPTIONAL — baked modules) ───────────────────
# These two modules run IN-PROCESS (the embedding task classifier and the
# memory backend). Their deps are baked at build time: WITH_ROUTER=1 installs
# the fastembed ONNX backend (no torch), WITH_MEMORY=1 adds lancedb. The
# runtime installer that used to write these into /opt/lcp-modules is gone.
#
# fastembed replaces sentence-transformers as the default embedder. It runs the
# SAME bge-small-en-v1.5 graph through ONNX: 999/1000 identical router outcome
# on real traffic, RSS 997 -> 306 MB, and it drops ~1.2 GB of torch + ~2.7 GB
# of CUDA wheels that a container with no /dev/nvidia* could never use.
# sentence-transformers is NOT installed any more. The code keeps a torch
# branch for model names fastembed does not ship, but it only works where
# torch is present (the bind-mounted /opt/lcp-modules/router legacy tree, or a
# custom image). In this image an unsupported model name fails with a clear
# MemoryError naming the backend rather than silently degrading.

# tokenizers is pinned to 0.22.2 explicitly: fastembed pulls it in
# transitively, and pinning keeps the tokenizer behaviour identical to the
# version the 999/1000 agreement measurement was made against.
#
# These install into the image's site-packages. The embedder probes its own
# backend lazily at first use, so a missing module surfaces as a clear
# MemoryError naming the backend rather than a crash during startup.
#
# fastembed + tokenizers are shared by BOTH modules; lancedb is memory-only.
# When baked, the embedding weights are cached under /app/models/embedding
# (not bind-mounted).
ARG WITH_ROUTER=0
ARG WITH_MEMORY=0
RUN if [ "$WITH_ROUTER" = "1" ] || [ "$WITH_MEMORY" = "1" ]; then \
        pip install --no-cache-dir fastembed "tokenizers==0.22.2" ; \
    fi \
    && if [ "$WITH_MEMORY" = "1" ]; then \
        pip install --no-cache-dir lancedb ; \
    fi \
    && if [ "$WITH_ROUTER" = "1" ]; then \
        mkdir -p /app/models/embedding \
        && python3 -c \
            "from fastembed import TextEmbedding; list(TextEmbedding(model_name='BAAI/bge-small-en-v1.5', cache_dir='/app/models/embedding/fastembed').embed(['warm']))" ; \
    fi

# ── Application code ──
COPY src/ ./src/
COPY config/ ./config/
# Public marketing landing page (served at /welcome — see handler._serve_landing).
COPY landing/ ./landing/
COPY alembic.ini .
COPY alembic/ ./alembic/
# Dev/ops helper scripts (judge_routing, probe_intent, seed_data, ...) so they
# can be run inside the container: docker compose exec lcp python3 scripts/...
COPY scripts/ ./scripts/

RUN mkdir -p /app/data

EXPOSE 8734

CMD ["sh", "-c", "cd /app && python3 -m alembic upgrade head && python3 -m src.main"]
