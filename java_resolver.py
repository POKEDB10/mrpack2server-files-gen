import os
import json
import time
import threading
from packaging.version import parse as parse_version
from java_versions import JAVA_VERSION_MAP  # Fallback hardcoded map

# Ordered fallback preference (highest to lowest)
FALLBACK_JAVA_VERSIONS = ["21", "17", "16", "11", "8"]

# Cache for dynamically resolved Java versions
_java_version_cache = {}  # {mc_version: {"java_version": str, "timestamp": float}}
_cache_lock = threading.Lock()
_cache_max_age = 7 * 24 * 3600  # 7 days

# Pattern-based Java version rules (for automatic detection)
# These rules are based on Minecraft's Java version requirements
JAVA_VERSION_RULES = [
    # Format: (min_version, max_version, java_version)
    # Versions 1.21+ require Java 21
    (parse_version("1.21"), parse_version("999.999"), "21"),
    # Versions 1.20.x - 1.19.x require Java 17
    (parse_version("1.19"), parse_version("1.20.999"), "17"),
    # Versions 1.18.x - 1.17.x require Java 16
    (parse_version("1.17"), parse_version("1.18.999"), "16"),
    # Versions 1.16.x require Java 8
    (parse_version("1.16"), parse_version("1.16.999"), "8"),
    # Versions < 1.16 require Java 8
    (parse_version("1.0"), parse_version("1.15.999"), "8"),
]

# Java installation base path - use /tmp/java for Render.com and Docker
# This is the writable location where setup_java.sh installs Java
JAVA_BASE_PATH = "/tmp/java"

# Fallback paths (for backwards compatibility and different environments)
JAVA_FALLBACK_PATHS = [
    os.path.expanduser("~/java"),  # User home directory
    os.path.join(os.getcwd(), "java"),  # Current directory
    "/opt/java",           # System directory (may be read-only)
]

def get_java_path(version):
    """Get Java path, checking /tmp/java first (where setup_java.sh installs it)."""
    # Primary path: /tmp/java (where setup_java.sh installs Java)
    primary_path = os.path.join(JAVA_BASE_PATH, f"java-{version}", "bin", "java")
    if os.path.exists(primary_path):
        return primary_path
    
    # Check fallback paths
    for base_path in JAVA_FALLBACK_PATHS:
        java_path = os.path.join(base_path, f"java-{version}", "bin", "java")
        if os.path.exists(java_path):
            return java_path
    
    # Fallback to old /opt/java-{version} location (for backwards compatibility)
    old_path = f"/opt/java-{version}/bin/java"
    if os.path.exists(old_path):
        return old_path
    
    # Return expected path even if not found (for error messages)
    return primary_path

def is_java_installed(version):
    """Check if Java is installed, checking /tmp/java first."""
    # Primary path: /tmp/java
    primary_path = os.path.join(JAVA_BASE_PATH, f"java-{version}", "bin", "java")
    if os.path.exists(primary_path):
        return True
    
    # Check fallback paths
    for base_path in JAVA_FALLBACK_PATHS:
        java_path = os.path.join(base_path, f"java-{version}", "bin", "java")
        if os.path.exists(java_path):
            return True
    
    # Check old /opt location for backwards compatibility
    if os.path.exists(f"/opt/java-{version}/bin/java"):
        return True
    
    return False

def log_installed_java_versions():
    found = [v for v in FALLBACK_JAVA_VERSIONS if is_java_installed(v)]
    print(f"🧩 Java versions installed: {', '.join(found) if found else 'None'}")

def get_java_version_from_pattern(mc_version):
    """Determine Java version based on Minecraft version pattern matching."""
    try:
        mc_ver = parse_version(mc_version)
        for min_ver, max_ver, java_ver in JAVA_VERSION_RULES:
            if min_ver <= mc_ver <= max_ver:
                return java_ver
    except Exception as e:
        print(f"⚠️ Pattern matching failed for '{mc_version}': {e}")
    return None

def get_java_version_from_mojang_api(mc_version):
    """Query Mojang's version API to get Java version requirement."""
    try:
        import requests
        session = requests.Session()
        
        # Get version manifest
        manifest_url = "https://launchermeta.mojang.com/mc/game/version_manifest.json"
        manifest_resp = session.get(manifest_url, timeout=10)
        manifest_resp.raise_for_status()
        manifest = manifest_resp.json()
        
        # Find the version
        version_info = None
        for version_entry in manifest.get('versions', []):
            if version_entry.get('id') == mc_version:
                version_info = version_entry
                break
        
        if not version_info:
            # Try to find a close match
            for version_entry in manifest.get('versions', []):
                version_id = version_entry.get('id', '')
                if version_id.startswith(mc_version) or mc_version.startswith(version_id.split('.')[0]):
                    version_info = version_entry
                    break
        
        if not version_info:
            return None
        
        # Get version details
        version_url = version_info.get('url')
        if not version_url:
            return None
        
        version_resp = session.get(version_url, timeout=10)
        version_resp.raise_for_status()
        version_data = version_resp.json()
        
        # Extract Java version from version data
        # Mojang stores Java version in javaVersion.majorVersion or similar
        java_version_info = version_data.get('javaVersion', {})
        if java_version_info:
            major_version = java_version_info.get('majorVersion')
            if major_version:
                return str(major_version)
        
        # Alternative: check if there's a javaVersion component
        if 'javaVersion' in version_data:
            java_ver = version_data['javaVersion']
            if isinstance(java_ver, dict):
                major = java_ver.get('majorVersion') or java_ver.get('component')
                if major:
                    return str(major)
        
        return None
    except Exception as e:
        print(f"⚠️ Failed to query Mojang API for Java version: {e}")
        return None

def resolve_java_version(loader_type, mc_version):
    """
    Intelligently resolve Java version for a given Minecraft version.
    Uses pattern matching, API queries, and cached results.
    Priority:
    1. Hardcoded map (fastest, most reliable)
    2. Cache (if recent)
    3. Pattern matching (works for most versions)
    4. Mojang API query (for newer/unknown versions)
    5. Version-based fallback rules
    6. Fallback to installed Java versions
    """
    loader_type = loader_type.lower()
    
    # Step 1: Try exact match in hardcoded map (fastest, most reliable)
    mapped = JAVA_VERSION_MAP.get(loader_type, {}).get(mc_version)
    if mapped:
        if is_java_installed(mapped):
            return mapped
        else:
            print(f"⚠️ Mapped Java {mapped} for {loader_type} {mc_version} not installed")
    
    # Step 2: Check cache
    with _cache_lock:
        if mc_version in _java_version_cache:
            cached_info = _java_version_cache[mc_version]
            # Check cache age
            if time.time() - cached_info.get('timestamp', 0) < _cache_max_age:
                cached_java = cached_info.get('java_version')
                if cached_java and is_java_installed(cached_java):
                    print(f"✅ Using cached Java version: {cached_java} for {mc_version}")
                    return cached_java
    
    # Step 3: Try pattern-based matching (works for most versions)
    pattern_java = get_java_version_from_pattern(mc_version)
    if pattern_java and is_java_installed(pattern_java):
        print(f"✅ Pattern-matched Java {pattern_java} for {mc_version}")
        # Cache the result
        with _cache_lock:
            _java_version_cache[mc_version] = {
                'java_version': pattern_java,
                'timestamp': time.time()
            }
        return pattern_java
    
    # Step 4: Query Mojang API for newer/unknown versions
    api_java = get_java_version_from_mojang_api(mc_version)
    if api_java and is_java_installed(api_java):
        print(f"✅ API-resolved Java {api_java} for {mc_version}")
        # Cache the result
        with _cache_lock:
            _java_version_cache[mc_version] = {
                'java_version': api_java,
                'timestamp': time.time()
            }
        return api_java
    
    # Step 5: Fallback to version-based rules (for older versions)
    try:
        if parse_version(mc_version) < parse_version("1.16") and is_java_installed("8"):
            return "8"
    except Exception as e:
        print(f"⚠️ Invalid version format '{mc_version}': {e}")
    
    # Step 6: Try fallback Java versions in order
    for version in FALLBACK_JAVA_VERSIONS:
        if is_java_installed(version):
            print(f"ℹ️ Falling back to Java {version} for {mc_version}")
            # Cache the fallback
            with _cache_lock:
                _java_version_cache[mc_version] = {
                    'java_version': version,
                    'timestamp': time.time()
                }
            return version
    
    # List all checked paths for debugging
    all_paths = [JAVA_BASE_PATH] + JAVA_FALLBACK_PATHS + ["/opt/java-{version}"]
    checked_paths = ", ".join(all_paths)
    raise RuntimeError(f"❌ No supported Java version found. Checked: {checked_paths}")
