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

# Install system packages with parallel downloads and clean up
RUN set -ex && \
    echo 'Acquire::Languages "none";' > /etc/apt/apt.conf.d/99translations && \
    echo 'APT::Install-Recommends "false";' > /etc/apt/apt.conf.d/99no-recommends && \
    DEBIAN_FRONTEND=noninteractive apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    unzip \
    curl \
    git \
    ca-certificates \
    build-essential \
    gcc \
    libffi-dev \
    libssl-dev \
    parallel \
    pigz \
    aria2 && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

# Copy only dependency files first (better layer caching)
COPY requirements.txt setup_java.sh ./

# Install Python dependencies with parallel processing
RUN pip install --no-cache-dir -U pip setuptools wheel && \
    pip install --no-cache-dir -r requirements.txt

# Setup Java in parallel (separate layer for better caching)
RUN bash setup_java.sh && rm -rf downloads

# Copy application code last (most frequently changed)
COPY . .

# Prepare startup script in same layer as copy
COPY start.sh /start.sh
RUN chmod +x /start.sh

# Expose the port
EXPOSE $PORT

# Optimized healthcheck (less aggressive)
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:$PORT/health || exit 1

# Note: For persistent count file storage, mount a volume and set COUNT_FILE_DIR env var:
# docker run -v /path/to/persistent/data:/data -e COUNT_FILE_DIR=/data ...
# Or use RENDER_DISK_PATH for Render deployments (configured in render.yaml)

# Start using the dynamic worker/thread script
CMD ["/start.sh"]