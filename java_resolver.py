import os
from packaging.version import parse as parse_version
from java_versions import JAVA_VERSION_MAP  # Your full map from the previous message

# Ordered fallback preference (highest to lowest)
FALLBACK_JAVA_VERSIONS = ["21", "17", "16", "11", "8"]

def get_java_path(version):
    return f"/tmp/java-{version}/bin/java"

def is_java_installed(version):
    return os.path.exists(get_java_path(version))

def log_installed_java_versions():
    found = [v for v in FALLBACK_JAVA_VERSIONS if is_java_installed(v)]
    print(f"🧩 Java versions installed: {', '.join(found) if found else 'None'}")

def resolve_java_version(loader_type, mc_version):
    loader_type = loader_type.lower()

    # Try exact match in the map
    mapped = JAVA_VERSION_MAP.get(loader_type, {}).get(mc_version)
    if mapped:
        if is_java_installed(mapped):
            return mapped
        else:
            print(f"⚠️ Mapped Java {mapped} for {loader_type} {mc_version} not installed")

    # Fallback: use Java 8 for < 1.16
    try:
        if parse_version(mc_version) < parse_version("1.16") and is_java_installed("8"):
            return "8"
    except Exception as e:
        print(f"⚠️ Invalid version format '{mc_version}': {e}")

    # Try fallbacks
    for version in FALLBACK_JAVA_VERSIONS:
        if is_java_installed(version):
            print(f"ℹ️ Falling back to Java {version}")
            return version

    raise RuntimeError("❌ No supported Java version found in /tmp.")
