#!/bin/bash
set -e

# Check for Render disk mount path (for persistent storage)
# Use Render disk mount if available, otherwise use temp directory
RENDER_DISK_PATH="${RENDER_DISK_PATH:-/opt/render/project/src/data}"

# Determine Java installation location (prioritize persistent storage)
if [ -d "$RENDER_DISK_PATH" ] && [ -w "$RENDER_DISK_PATH" ]; then
  # Use Render disk mount for persistent storage
  JAVA_BASE="$RENDER_DISK_PATH/java"
  echo "📦 Using persistent storage: $JAVA_BASE"
elif [ -w "/tmp" ]; then
  # Fallback to /tmp (ephemeral, but always writable)
  JAVA_BASE="/tmp/java"
  echo "📦 Using temporary storage: $JAVA_BASE"
elif [ -w "$HOME" ]; then
  # Fallback to home directory
  JAVA_BASE="$HOME/java"
  echo "📦 Using home directory: $JAVA_BASE"
else
  # Last resort: use current directory
  JAVA_BASE="$(pwd)/java"
  echo "📦 Using current directory: $JAVA_BASE"
fi

mkdir -p downloads
mkdir -p "$JAVA_BASE"

install_java() {
  version=$1
  url=$2
  dest="$JAVA_BASE/java-${version}"

  if [ -d "$dest" ] && [ -f "$dest/bin/java" ]; then
    echo "✅ Java $version already installed at $dest"
    return
  fi

  echo "📦 Installing Java $version to $dest..."
  file="downloads/java-${version}.tar.gz"

  if [ ! -f "$file" ]; then
    echo "⬇️ Downloading Java $version..."
    curl -sSfL -o "$file" "$url"
  fi

  mkdir -p "$dest"
  echo "📦 Extracting Java $version..."
  tar -xzf "$file" -C "$dest" --strip-components=1
  
  # Verify installation
  if [ -f "$dest/bin/java" ]; then
    echo "✅ Java $version installed to $dest"
  else
    echo "❌ Java $version installation failed - java binary not found"
    return 1
  fi
}

install_java 8  "https://github.com/adoptium/temurin8-binaries/releases/download/jdk8u412-b08/OpenJDK8U-jdk_x64_linux_hotspot_8u412b08.tar.gz" &
install_java 11 "https://github.com/adoptium/temurin11-binaries/releases/download/jdk-11.0.23+9/OpenJDK11U-jdk_x64_linux_hotspot_11.0.23_9.tar.gz" &
install_java 16 "https://github.com/adoptium/temurin16-binaries/releases/download/jdk-16.0.2+7/OpenJDK16U-jdk_x64_linux_hotspot_16.0.2_7.tar.gz" &
install_java 17 "https://github.com/adoptium/temurin17-binaries/releases/download/jdk-17.0.11+9/OpenJDK17U-jdk_x64_linux_hotspot_17.0.11_9.tar.gz" &
install_java 21 "https://github.com/adoptium/temurin21-binaries/releases/download/jdk-21.0.3+9/OpenJDK21U-jdk_x64_linux_hotspot_21.0.3_9.tar.gz" &

wait
echo "✅ All Java versions installed."

