FROM python:3.11-slim

WORKDIR /app

# Install Java from Debian repos (simplest, most reliable)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        openjdk-8-jre-headless \
        openjdk-11-jre-headless \
        openjdk-17-jre-headless \
        openjdk-21-jre-headless \
        curl wget unzip git \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /tmp/java \
    && ln -sf /usr/lib/jvm/java-8-openjdk-amd64 /tmp/java/java-8 \
    && ln -sf /usr/lib/jvm/java-11-openjdk-amd64 /tmp/java/java-11 \
    && ln -sf /usr/lib/jvm/java-17-openjdk-amd64 /tmp/java/java-17 \
    && ln -sf /usr/lib/jvm/java-21-openjdk-amd64 /tmp/java/java-21

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /tmp/servers /tmp/forge_cache /tmp/quilt_cache /app/config

EXPOSE 8090

CMD ["gunicorn", "-k", "geventwebsocket.gunicorn.workers.GeventWebSocketWorker", \
     "-w", "1", "--bind", "0.0.0.0:8090", "--timeout", "300", "app:app"]