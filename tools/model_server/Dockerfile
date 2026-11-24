# Lynxx gender model server (python -m lynxx.model_server, docs/operations.md §3c)
#   docker build -f tools/model_server/Dockerfile -t lynxx-model-server .
#   docker run -d --name lynxx-model -e LYNXX_GENDER_MODEL_TOKEN=<token> -p 127.0.0.1:8765:8765 \
#     -v lynxx-model:/models lynxx-model-server
# The model (~380 MB, pinned revision, sha256 checked) is downloaded into the /models volume on the first start.
# Listening on 0.0.0.0 inside the container needs a token (the server refuses to start without one).
# NVIDIA GPU (needs the NVIDIA Container Toolkit on the host; CUDA / cuDNN come from pip, ~2.4 GB more):
#   docker build -f tools/model_server/Dockerfile --build-arg ORT_PACKAGE='onnxruntime-gpu[cuda,cudnn]' -t lynxx-model-server:gpu .
#   docker run -d --gpus all --name lynxx-model -e LYNXX_GENDER_MODEL_TOKEN=<token> -p 127.0.0.1:8765:8765 \
#     -v lynxx-model:/models lynxx-model-server:gpu
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LYNXX_GENDER_MODEL_DIR=/models \
    LYNXX_DATA_DIR=/data

# ffmpeg: frames of videos for POST /v1/frames (-gender_verify_media); animated pictures only need Pillow
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# the server needs only these: the downloader dependencies of requirements.txt are not used
ARG ORT_PACKAGE=onnxruntime>=1.18
RUN pip install --no-cache-dir 'requests>=2.31' 'numpy>=1.26' "${ORT_PACKAGE}" 'pillow>=10'

WORKDIR /srv
COPY lynxx ./lynxx
COPY LICENSE THIRD_PARTY_NOTICES.md ./
RUN useradd --system --uid 10001 --home-dir /data lynxx \
    && mkdir -p /models /data \
    && chown lynxx /models /data
USER lynxx

VOLUME /models
EXPOSE 8765
HEALTHCHECK --interval=60s --timeout=5s --start-period=600s \
    CMD python -c "import os, urllib.request as u; r = u.Request('http://127.0.0.1:8765/v1/status', headers={'Authorization': 'Bearer ' + os.environ.get('LYNXX_GENDER_MODEL_TOKEN', '')}); u.urlopen(r, timeout=4)"
CMD ["python", "-m", "lynxx.model_server", "--host", "0.0.0.0", "--port", "8765"]
