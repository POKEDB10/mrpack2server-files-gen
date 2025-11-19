#!/bin/bash
set -e

echo "🔧 Setting up Java symlinks..."

# 1. Ensure /tmp/java exists
mkdir -p /tmp/java

# 2. Dynamic Java Discovery Function
link_java() {
    local version=$1
    # Search for directories containing the version number in /usr/lib/jvm
    # This matches 'java-8-openjdk', 'temurin-8-jdk', 'temurin-16-jdk', etc.
    local pattern="*-$version-*"
    
    # Find the directory
    local target=$(find /usr/lib/jvm -maxdepth 1 -name "$pattern" -type d | head -n 1)
    
    if [ -n "$target" ] && [ -d "$target" ]; then
        echo "🔗 Found Java $version at: $target"
        ln -sf "$target" "/tmp/java/java-$version"
    else
        echo "⚠️ Could not find installation directory for Java $version in /usr/lib/jvm"
    fi
}

# 3. Create the links dynamically
link_java 8
link_java 11
link_java 16
link_java 17
link_java 21

# 4. Verify installations (Critical Step)
echo "🔍 Verifying Java Binaries..."
has_error=0
for ver in 8 11 16 17 21; do
    if [ -x "/tmp/java/java-$ver/bin/java" ]; then
        echo "✅ Java $ver is working."
    else
        echo "❌ Java $ver NOT found or not executable."
        has_error=1
    fi
done

# 5. Calculate Workers
CPUS=$(nproc)
WORKERS=$((2 * CPUS + 1))
if [ "$WORKERS" -gt 10 ]; then WORKERS=10; fi
if [ "$WORKERS" -lt 2 ]; then WORKERS=2; fi 

echo "🧠 Detected $CPUS cores, launching with $WORKERS workers."

# 6. Primary Worker Check
if (set -C; echo $$ > "/tmp/msfg_primary_worker.lock") 2>/dev/null; then
    export PRIMARY_WORKER=1
    echo "👑 This is the Primary Worker"
else
    export PRIMARY_WORKER=0
    echo "👷 This is a Secondary Worker"
fi

# 7. Start Gunicorn
exec gunicorn app:app \
    --bind 0.0.0.0:${PORT:-8090} \
    --workers "$WORKERS" \
    --worker-class geventwebsocket.gunicorn.workers.GeventWebSocketWorker \
    --timeout 300 \
    --keep-alive 30 \
    --access-logfile - \
    --error-logfile -