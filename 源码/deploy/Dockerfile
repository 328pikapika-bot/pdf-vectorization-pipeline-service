FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN pip install --no-cache-dir -i "${PIP_INDEX_URL}" -r requirements.txt

COPY src ./src
COPY config ./config

EXPOSE 18080

ENTRYPOINT ["python", "src/pipeline.py"]
CMD ["--config", "/app/config/config.yaml", "--auto-retry"]
