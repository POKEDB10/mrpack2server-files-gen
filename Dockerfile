# Use a lightweight Python base image
FROM python:3.11-slim

# Set environment vars
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PORT=8080

# Create app dir
WORKDIR /app

# Install base system packages + curl
RUN apt-get update && apt-get install -y \
    unzip \
    curl \
    git \
    gnupg \
    ca-certificates \
    libglib2.0-0 \
    libxext6 \
    libsm6 \
    libxrender1 \
    && rm -rf /var/lib/apt/lists/*

# Copy app files
COPY . /app

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Download and extract Java versions to /opt/java-<version>
RUN bash setup_java.sh

# Expose the port (Render sets $PORT)
EXPOSE $PORT

# Start the Flask app using Gunicorn
CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:8080", "--timeout", "300", "--workers", "4"]
