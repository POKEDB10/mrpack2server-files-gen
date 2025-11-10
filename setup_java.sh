#!/bin/bash
set -e
JAVA_BASE="/opt"
mkdir -p downloads

install_java() {
  version=$1
  url=$2
  dest="$JAVA_BASE/java-${version}"

  if [ -d "$dest" ]; then
    echo "✅ Java $version already installed"
    return
  fi

  echo "📦 Installing Java $version..."
  file="downloads/java-${version}.tar.gz"

  if [ ! -f "$file" ]; then
    curl -sSfL -o "$file" "$url"
  fi

  mkdir -p "$dest"
  tar -xzf "$file" -C "$dest" --strip-components=1
  echo "✅ Java $version installed to $dest"
}

install_java 8  "https://github.com/adoptium/temurin8-binaries/releases/download/jdk8u412-b08/OpenJDK8U-jdk_x64_linux_hotspot_8u412b08.tar.gz" &
install_java 11 "https://github.com/adoptium/temurin11-binaries/releases/download/jdk-11.0.23+9/OpenJDK11U-jdk_x64_linux_hotspot_11.0.23_9.tar.gz" &
install_java 16 "https://github.com/adoptium/temurin16-binaries/releases/download/jdk-16.0.2+7/OpenJDK16U-jdk_x64_linux_hotspot_16.0.2_7.tar.gz" &
install_java 17 "https://github.com/adoptium/temurin17-binaries/releases/download/jdk-17.0.11+9/OpenJDK17U-jdk_x64_linux_hotspot_17.0.11_9.tar.gz" &
install_java 21 "https://github.com/adoptium/temurin21-binaries/releases/download/jdk-21.0.3+9/OpenJDK21U-jdk_x64_linux_hotspot_21.0.3_9.tar.gz" &

wait
echo "✅ All Java versions installed."

