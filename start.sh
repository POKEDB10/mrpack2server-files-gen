#!/bin/bash
set -e

echo "🔧 Setting up Java symlinks..."

# 1. Ensure /tmp/java exists
mkdir -p /tmp/java

# 2. Create symlinks to the Docker-installed Java versions
# We use -f to force overwrite in case the container restarted but /tmp persisted
ln -sf /usr/lib/jvm/java-8-openjdk-amd64 /tmp/java/java-8
ln -sf /usr/lib/jvm/java-11-openjdk-amd64 /tmp/java/java-11
ln -sf /usr/lib/jvm/java-17-openjdk-amd64 /tmp/java/java-17
ln -sf /usr/lib/jvm/java-21-openjdk-amd64 /tmp/java/java-21

# 3. Verify installations (Critical Step)
echo "🔍 Verifying Java Binaries..."
has_error=0
for ver in 8 11 17 21; do
    if [ -x "/tmp/java/java-$ver/bin/java" ]; then
        echo "✅ Java $ver found."
    else
        echo "❌ Java $ver NOT found at /tmp/java/java-$ver/bin/java"
        has_error=1
    fi
done

if [ $has_error -eq 1 ]; then
    echo "⚠️ Some Java versions are missing. The server generator may fail for those versions."
    # We don't exit here because maybe the user only needs Java 17, 
    # but it's good to see in the logs.
fi

# 4. Calculate Workers
CPUS=$(nproc)
WORKERS=$((2 * CPUS + 1))
if [ "$WORKERS" -gt 10 ]; then WORKERS=10; fi
if [ "$WORKERS" -lt 2 ]; then WORKERS=2; fi # Ensure at least 2 workers

echo "🧠 Detected $CPUS cores, launching with $WORKERS workers."

# 5. Primary Worker Check (Local container scope only)
if (set -C; echo $$ > "/tmp/msfg_primary_worker.lock") 2>/dev/null; then
    export PRIMARY_WORKER=1
    echo "👑 This is the Primary Worker"
else
    export PRIMARY_WORKER=0
    echo "👷 This is a Secondary Worker"
fi

# 6. Start Gunicorn
# Ensure the gevent worker is used for WebSockets
exec gunicorn app:app \
    --bind 0.0.0.0:${PORT:-8090} \
    --workers "$WORKERS" \
    --worker-class geventwebsocket.gunicorn.workers.GeventWebSocketWorker \
    --timeout 300 \
    --keep-alive 30 \
    --access-logfile - \
    --error-logfile -