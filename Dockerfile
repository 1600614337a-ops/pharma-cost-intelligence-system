FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/app \
    MPLCONFIGDIR=/tmp/matplotlib \
    COST_PDF_CONVERTER=libreoffice \
    COST_LIBREOFFICE_PATH=/usr/bin/libreoffice

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        fontconfig \
        fonts-noto-cjk \
        libreoffice-writer \
        poppler-utils \
        tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/app
COPY requirements.txt requirements-llamaindex.txt ./
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts

WORKDIR /workspace
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=4s --start-period=60s --retries=4 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3)" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "python", "/opt/app/scripts/container_start.py"]
CMD ["--project-root", "/workspace", "--code-root", "/opt/app"]
