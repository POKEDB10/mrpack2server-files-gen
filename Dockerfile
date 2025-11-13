# Use a specific version of the lightweight Python base image
FROM python:3.11.2-slim

# Set environment vars
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8090 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Create app dir
WORKDIR /app

# Install system packages including Java (all versions needed for Minecraft)
RUN set -ex && \
    echo 'Acquire::Languages "none";' > /etc/apt/apt.conf.d/99translations && \
    echo 'APT::Install-Recommends "false";' > /etc/apt/apt.conf.d/99no-recommends && \
    DEBIAN_FRONTEND=noninteractive apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    unzip \
    curl \
    wget \
    git \
    ca-certificates \
    build-essential \
    gcc \
    libffi-dev \
    libssl-dev \
    openjdk-8-jre-headless \
    openjdk-11-jre-headless \
    openjdk-17-jre-headless \
    openjdk-21-jre-headless && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

# Create Java symlinks where the app expects them
RUN mkdir -p /tmp/java && \
    ln -sf /usr/lib/jvm/java-8-openjdk-amd64 /tmp/java/java-8 && \
    ln -sf /usr/lib/jvm/java-11-openjdk-amd64 /tmp/java/java-11 && \
    ln -sf /usr/lib/jvm/java-17-openjdk-amd64 /tmp/java/java-17 && \
    ln -sf /usr/lib/jvm/java-21-openjdk-amd64 /tmp/java/java-21

# Copy only dependency files first (better layer caching)
COPY requirements.txt ./

# Install Python dependencies
RUN pip install --no-cache-dir -U pip setuptools wheel && \
    pip install --no-cache-dir -r requirements.txt

# Copy application code last (most frequently changed)
COPY . .

# Create necessary directories
RUN mkdir -p /tmp/servers /tmp/forge_cache /tmp/quilt_cache /app/config && \
    chmod -R 777 /tmp/servers /tmp/forge_cache /tmp/quilt_cache /app/config

# Make startup script executable
RUN chmod +x start.sh

# Expose the port
EXPOSE $PORT

# Optimized healthcheck (less aggressive)
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:$PORT/ || exit 1

# Note: For persistent count file storage, mount a volume and set COUNT_FILE_DIR env var:
# docker run -v /path/to/persistent/data:/data -e COUNT_FILE_DIR=/data ...
# Or use RENDER_DISK_PATH for Render deployments (configured in render.yaml)

# Start using the dynamic worker/thread script
CMD ["./start.sh"]