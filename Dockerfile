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

# Install dependencies and Add Adoptium (Temurin) Repo for Java
RUN set -ex && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
    wget \
    gnupg \
    ca-certificates \
    unzip \
    curl \
    git \
    build-essential \
    libffi-dev \
    libssl-dev && \
    # Add Adoptium GPG key and Repo
    mkdir -p /etc/apt/keyrings && \
    wget -O - https://packages.adoptium.net/artifactory/api/gpg/key/public | tee /etc/apt/keyrings/adoptium.asc && \
    echo "deb [signed-by=/etc/apt/keyrings/adoptium.asc] https://packages.adoptium.net/artifactory/deb $(awk -F= '/^VERSION_CODENAME/{print$2}' /etc/os-release) main" | tee /etc/apt/sources.list.d/adoptium.list && \
    apt-get update && \
    # Install LTS Java versions via APT
    apt-get install -y \
    temurin-8-jdk \
    temurin-11-jdk \
    temurin-17-jdk \
    temurin-21-jdk && \
    # Install Java 16 Manually (EOL version, not in apt)
    wget -O /tmp/java16.tar.gz https://github.com/adoptium/temurin16-binaries/releases/download/jdk-16.0.2%2B7/OpenJDK16U-jdk_x64_linux_hotspot_16.0.2_7.tar.gz && \
    mkdir -p /usr/lib/jvm/temurin-16-jdk && \
    tar -xzf /tmp/java16.tar.gz -C /usr/lib/jvm/temurin-16-jdk --strip-components=1 && \
    rm /tmp/java16.tar.gz && \
    # Cleanup to keep image small
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

# Create Java symlinks directory (populated by start.sh)
RUN mkdir -p /tmp/java

# Copy only dependency files first
COPY requirements.txt ./

# Install Python dependencies
RUN pip install --no-cache-dir -U pip setuptools wheel && \
    pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create necessary directories
RUN mkdir -p /tmp/servers /tmp/forge_cache /tmp/quilt_cache /app/config && \
    chmod -R 777 /tmp/servers /tmp/forge_cache /tmp/quilt_cache /app/config

# Make startup script executable
RUN chmod +x start.sh

# Expose the port
EXPOSE $PORT

# Healthcheck
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:$PORT/ || exit 1

# Start command
CMD ["./start.sh"]