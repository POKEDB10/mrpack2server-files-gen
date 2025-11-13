#!/bin/bash
set -e

# Determine Java installation location
RENDER_DISK_PATH="${RENDER_DISK_PATH:-/opt/render/project/src/data}"

if [ -d "$RENDER_DISK_PATH" ] && [ -w "$RENDER_DISK_PATH" ]; then
  JAVA_BASE="$RENDER_DISK_PATH/java"
  echo "📦 Using persistent storage: $JAVA_BASE"
else
  JAVA_BASE="/tmp/java"
  echo "📦 Using temporary storage: $JAVA_BASE"
fi

mkdir -p "$JAVA_BASE"

install_java() {
  version=$1
  url=$2
  dest="$JAVA_BASE/java-${version}"

  if [ -d "$dest" ] && [ -f "$dest/bin/java" ]; then
    echo "✅ Java $version already installed"
    return 0
  fi

  echo "📦 Installing Java $version..."
  tmp_file=$(mktemp)
  
  if curl -sSfL -o "$tmp_file" "$url"; then
    mkdir -p "$dest"
    tar -xzf "$tmp_file" -C "$dest" --strip-components=1
    rm -f "$tmp_file"
    
    if [ -f "$dest/bin/java" ]; then
      echo "✅ Java $version installed"
      "$dest/bin/java" -version 2>&1 | head -n 1
      return 0
    else
      echo "❌ Java $version installation failed"
      rm -rf "$dest"
      return 1
    fi
  else
    echo "❌ Failed to download Java $version"
    rm -f "$tmp_file"
    return 1
  fi
}

# Install all Java versions (sequentially to avoid overwhelming the system)
install_java 8  "https://github.com/adoptium/temurin8-binaries/releases/download/jdk8u412-b08/OpenJDK8U-jdk_x64_linux_hotspot_8u412b08.tar.gz"
install_java 11 "https://github.com/adoptium/temurin11-binaries/releases/download/jdk-11.0.23+9/OpenJDK11U-jdk_x64_linux_hotspot_11.0.23_9.tar.gz"
install_java 17 "https://github.com/adoptium/temurin17-binaries/releases/download/jdk-17.0.11+9/OpenJDK17U-jdk_x64_linux_hotspot_17.0.11_9.tar.gz"
install_java 21 "https://github.com/adoptium/temurin21-binaries/releases/download/jdk-21.0.3+9/OpenJDK21U-jdk_x64_linux_hotspot_21.0.3_9.tar.gz"

echo "✅ All Java versions installed successfully"