#!/bin/bash
set -e

echo "🔧 Setting up Java symlinks..."

# Create /tmp/java directory and symlinks to system Java installations
mkdir -p /tmp/java

# Create symlinks if they don't exist
[ ! -e /tmp/java/java-8 ] && ln -sf /usr/lib/jvm/java-8-openjdk-amd64 /tmp/java/java-8
[ ! -e /tmp/java/java-11 ] && ln -sf /usr/lib/jvm/java-11-openjdk-amd64 /tmp/java/java-11
[ ! -e /tmp/java/java-17 ] && ln -sf /usr/lib/jvm/java-17-openjdk-amd64 /tmp/java/java-17
[ ! -e /tmp/java/java-21 ] && ln -sf /usr/lib/jvm/java-21-openjdk-amd64 /tmp/java/java-21

# Verify Java installations
echo "✅ Java 8: $(test -x /tmp/java/java-8/bin/java && echo 'OK' || echo 'MISSING')"
echo "✅ Java 11: $(test -x /tmp/java/java-11/bin/java && echo 'OK' || echo 'MISSING')"
echo "✅ Java 17: $(test -x /tmp/java/java-17/bin/java && echo 'OK' || echo 'MISSING')"
echo "✅ Java 21: $(test -x /tmp/java/java-21/bin/java && echo 'OK' || echo 'MISSING')"

# Calculate optimal Gunicorn workers (recommended: 2*CPUs + 1)
# For free tier, limit workers to avoid memory issues
CPUS=$(nproc)
WORKERS=$((2 * CPUS + 1))

# Limit workers for free tier (Render free plan has limited resources)
# On Render free tier, typically 1 CPU, so this will be 3 workers max
# For local development with many cores, cap at reasonable number
if [ "$WORKERS" -gt 10 ]; then
    WORKERS=10
fi

# Cap threads (not used with gevent, but kept for reference)
THREADS=$((CPUS * 2))
if [ "$THREADS" -lt 4 ]; then
  THREADS=4
elif [ "$THREADS" -gt 8 ]; then
  THREADS=8
fi

echo "🧠 Detected $CPUS cores, launching with $WORKERS workers (gevent-websocket)"

# Function to determine if this is the primary worker
is_primary_worker() {
    # Try to acquire a lock on a file in /tmp
    local lock_file="/tmp/msfg_primary_worker.lock"
    
    # Try to create and lock the file
    if (set -C; echo $$ > "$lock_file") 2>/dev/null; then
        # We got the lock, we're the primary worker
        echo "Primary worker started with PID $$"
        export PRIMARY_WORKER=1
        return 0
    else
        # Another process already has the lock
        echo "Secondary worker started with PID $$"
        export PRIMARY_WORKER=0
        return 1
    fi
}

# Determine if this is the primary worker
is_primary_worker

# Start Gunicorn with gevent websocket worker
# Use PORT from environment (Render.com provides this)
exec gunicorn app:app \
    --bind 0.0.0.0:${PORT:-8090} \
    --workers "$WORKERS" \
    --worker-class geventwebsocket.gunicorn.workers.GeventWebSocketWorker \
    --timeout 300 \
    --access-logfile - \
    --error-logfile -