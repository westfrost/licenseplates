FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

# libglib is needed by some opencv-headless builds
RUN apt-get update && apt-get install -y --no-install-recommends libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY driveway_lpr ./driveway_lpr

# Download the ALPR models at build time so the container also works offline.
RUN python -c "from driveway_lpr.recognizer import FastAlprRecognizer as R; \
R('yolo-v9-t-384-license-plate-end2end', 'cct-xs-v2-global-model', 0.4, [0, 0, 1, 1])"

EXPOSE 8080
VOLUME ["/app/data"]
ENTRYPOINT ["python", "-m", "driveway_lpr", "-c", "/app/config.yaml"]
CMD ["run"]
