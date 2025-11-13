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

# Check for Render disk mount path (for persistent storage)
# Use Render disk mount if available, otherwise use temp directory
RENDER_DISK_PATH = os.environ.get("RENDER_DISK_PATH", "/opt/render/project/src/data")

# Java installation base path - prioritize persistent storage on Render
# Check RENDER_DISK_PATH first (persistent), then fallback to /tmp (ephemeral)
if os.path.exists(RENDER_DISK_PATH) and os.access(RENDER_DISK_PATH, os.W_OK):
    JAVA_BASE_PATH = os.path.join(RENDER_DISK_PATH, "java")
else:
    JAVA_BASE_PATH = "/tmp/java"

# Fallback paths (for backwards compatibility and different environments)
JAVA_FALLBACK_PATHS = []

# Add Render disk path if it's different from primary (and exists)
render_java_path = os.path.join(RENDER_DISK_PATH, "java")
if render_java_path != JAVA_BASE_PATH and os.path.exists(RENDER_DISK_PATH) and os.access(RENDER_DISK_PATH, os.W_OK):
    JAVA_FALLBACK_PATHS.append(render_java_path)

# Add other fallback paths
JAVA_FALLBACK_PATHS.extend([
    os.path.expanduser("~/java"),  # User home directory
    os.path.join(os.getcwd(), "java"),  # Current directory
    "/tmp/java",  # Temp directory (ephemeral)
    "/opt/java",  # System directory (may be read-only)
])


def debug_java_paths(version):
    """Debug function to see what's actually in Java directories."""
    base_path = os.path.join(JAVA_BASE_PATH, f"java-{version}")
    print(f"\n🔍 Debugging Java {version} at {base_path}")
    
    if os.path.exists(base_path):
        print(f"  ✅ Directory exists")
        
        # Check if it's a symlink
        if os.path.islink(base_path):
            target = os.readlink(base_path)
            print(f"  🔗 Symlink points to: {target}")
            
            # Check if target exists
            if os.path.exists(target):
                print(f"  ✅ Symlink target exists")
                
                # List contents of target
                try:
                    contents = os.listdir(target)
                    print(f"  📁 Target contents: {', '.join(contents[:10])}")  # First 10 items
                    
                    # Check for bin directory
                    bin_path = os.path.join(target, "bin")
                    if os.path.exists(bin_path):
                        print(f"  ✅ bin/ directory exists")
                        bin_contents = os.listdir(bin_path)
                        print(f"  📁 bin/ contents: {', '.join(bin_contents[:10])}")
                        
                        # Check for java executable
                        java_path = os.path.join(bin_path, "java")
                        if os.path.exists(java_path):
                            print(f"  ✅ java executable exists at {java_path}")
                            print(f"  🔐 Executable: {os.access(java_path, os.X_OK)}")
                        else:
                            print(f"  ❌ java executable NOT found at {java_path}")
                    else:
                        print(f"  ❌ bin/ directory NOT found")
                except Exception as e:
                    print(f"  ❌ Error listing contents: {e}")
            else:
                print(f"  ❌ Symlink target does NOT exist")
        else:
            print(f"  📁 Regular directory (not a symlink)")
            
            # List contents of the directory
            try:
                contents = os.listdir(base_path)
                print(f"  📁 Directory contents: {', '.join(contents[:10])}")  # First 10 items
                
                # Check for bin directory
                bin_path = os.path.join(base_path, "bin")
                if os.path.exists(bin_path):
                    print(f"  ✅ bin/ directory exists")
                    bin_contents = os.listdir(bin_path)
                    print(f"  📁 bin/ contents: {', '.join(bin_contents[:10])}")
                    
                    # Check for java executable
                    java_path = os.path.join(bin_path, "java")
                    if os.path.exists(java_path):
                        print(f"  ✅ java executable exists at {java_path}")
                        print(f"  🔐 Executable: {os.access(java_path, os.X_OK)}")
                    else:
                        print(f"  ❌ java executable NOT found at {java_path}")
                else:
                    print(f"  ❌ bin/ directory NOT found")
            except Exception as e:
                print(f"  ❌ Error listing contents: {e}")
    else:
        print(f"  ❌ Directory does NOT exist")


def get_java_path(version):
    """Get Java path, checking persistent storage first (RENDER_DISK_PATH), then /tmp/java."""
    # First check if the Java directory exists
    java_dir = os.path.join(JAVA_BASE_PATH, f"java-{version}")
    if not os.path.exists(java_dir):
        # Check fallback paths
        for base_path in JAVA_FALLBACK_PATHS:
            java_dir = os.path.join(base_path, f"java-{version}")
            if os.path.exists(java_dir):
                break
        else:
            # Check old /opt location for backwards compatibility
            if os.path.exists(f"/opt/java-{version}"):
                java_dir = f"/opt/java-{version}"
            else:
                # Return expected path even if not found (for error messages)
                return os.path.join(JAVA_BASE_PATH, f"java-{version}", "bin", "java")
    
    # Now look for the java executable in various possible locations
    for bin_path in ["bin", "jre/bin", "jdk/bin"]:
        java_path = os.path.join(java_dir, bin_path, "java")
        if os.path.exists(java_path) and os.access(java_path, os.X_OK):
            return java_path
    
    # Return the first possible path if none found
    return os.path.join(java_dir, "bin", "java")


def is_java_installed(version):
    """Check if Java is installed, checking persistent storage first (RENDER_DISK_PATH), then /tmp/java."""
    # First check if the Java directory exists
    java_dir = os.path.join(JAVA_BASE_PATH, f"java-{version}")
    if not os.path.exists(java_dir):
        # Check fallback paths
        for base_path in JAVA_FALLBACK_PATHS:
            java_dir = os.path.join(base_path, f"java-{version}")
            if os.path.exists(java_dir):
                break
        else:
            # Check old /opt location for backwards compatibility
            if os.path.exists(f"/opt/java-{version}"):
                java_dir = f"/opt/java-{version}"
            else:
                return False
    
    # Now check for the java executable in various possible locations
    for bin_path in ["bin", "jre/bin", "jdk/bin"]:
        java_path = os.path.join(java_dir, bin_path, "java")
        if os.path.exists(java_path) and os.access(java_path, os.X_OK):
            return True
    
    return False


def _copy_java_to_persistent_storage():
    """Copy Java installations from Docker build location (/tmp/java) to persistent storage (RENDER_DISK_PATH/java)."""
    # Only copy if RENDER_DISK_PATH is available and writable
    if not os.path.exists(RENDER_DISK_PATH) or not os.access(RENDER_DISK_PATH, os.W_OK):
        return False
    
    persistent_java_path = os.path.join(RENDER_DISK_PATH, "java")
    build_java_path = "/tmp/java"
    
    # If persistent location already has Java, skip copying
    if os.path.exists(persistent_java_path):
        try:
            contents = os.listdir(persistent_java_path)
            # Check if any Java versions exist and have the java binary
            java_versions = []
            for d in contents:
                if d.startswith("java-") and os.path.isdir(os.path.join(persistent_java_path, d)):
                    java_bin = os.path.join(persistent_java_path, d, "bin", "java")
                    if os.path.exists(java_bin):
                        java_versions.append(d)
            if java_versions:
                # Java already exists in persistent storage
                return True
        except Exception:
            pass
    
    # Check if Java exists in build location
    if not os.path.exists(build_java_path):
        return False
    
    try:
        build_contents = os.listdir(build_java_path)
        java_versions = [d for d in build_contents if d.startswith("java-") and os.path.isdir(os.path.join(build_java_path, d))]
        
        if not java_versions:
            return False
        
        # Create persistent Java directory
        os.makedirs(persistent_java_path, exist_ok=True)
        
        # Copy each Java version
        import shutil
        copied = []
        for java_version in java_versions:
            src = os.path.join(build_java_path, java_version)
            dst = os.path.join(persistent_java_path, java_version)
            
            # Skip if already exists in destination and has java binary
            if os.path.exists(dst):
                java_bin = os.path.join(dst, "bin", "java")
                if os.path.exists(java_bin):
                    continue
            
            try:
                print(f"📦 Copying {java_version} from build location to persistent storage...")
                if os.path.exists(dst):
                    # Remove existing incomplete installation
                    shutil.rmtree(dst)
                shutil.copytree(src, dst)
                # Verify the copy was successful
                java_bin = os.path.join(dst, "bin", "java")
                if os.path.exists(java_bin):
                    copied.append(java_version)
                else:
                    print(f"⚠️ Copy of {java_version} failed - java binary not found in destination")
                    if os.path.exists(dst):
                        shutil.rmtree(dst)
            except Exception as e:
                print(f"⚠️ Failed to copy {java_version}: {e}")
                # Clean up partial copy
                if os.path.exists(dst):
                    try:
                        shutil.rmtree(dst)
                    except Exception:
                        pass
        
        if copied:
            print(f"✅ Copied {len(copied)} Java version(s) to persistent storage: {', '.join(copied)}")
            return True
        return False
    except Exception as e:
        print(f"⚠️ Failed to copy Java to persistent storage: {e}")
        return False

def log_installed_java_versions():
    """Log all installed Java versions with their paths.
    Also attempts to copy Java from build location to persistent storage if needed."""
    # Try to copy Java from build location to persistent storage first
    # This ensures Java is available in persistent storage for Render.com deployments
    copied = _copy_java_to_persistent_storage()
    
    # After copying (if successful), re-check Java installation
    # This forces re-evaluation of JAVA_BASE_PATH after copy
    found = []
    for version in FALLBACK_JAVA_VERSIONS:
        if is_java_installed(version):
            java_path = get_java_path(version)
            found.append(f"{version} ({java_path})")
    
    if found:
        if copied:
            print(f"🧩 Java versions installed: {', '.join(found)} (copied to persistent storage)")
        else:
            print(f"🧩 Java versions installed: {', '.join(found)}")
    else:
        print(f"⚠️ No Java versions found. Checked base path: {JAVA_BASE_PATH}")
        # List what's actually in the base path
        if os.path.exists(JAVA_BASE_PATH):
            try:
                contents = os.listdir(JAVA_BASE_PATH)
                if contents:
                    print(f"   Directory {JAVA_BASE_PATH} contains: {', '.join(contents)}")
                else:
                    print(f"   Directory {JAVA_BASE_PATH} is empty")
            except Exception as e:
                print(f"   Could not list {JAVA_BASE_PATH}: {e}")
        else:
            print(f"   Directory {JAVA_BASE_PATH} does not exist")
        
        # Also check build location
        build_java_path = "/tmp/java"
        if os.path.exists(build_java_path):
            try:
                build_contents = os.listdir(build_java_path)
                java_versions = [d for d in build_contents if d.startswith("java-")]
                if java_versions:
                    print(f"   Found Java in build location {build_java_path}: {', '.join(java_versions)}")
                    print(f"   RENDER_DISK_PATH is available: {os.path.exists(RENDER_DISK_PATH) and os.access(RENDER_DISK_PATH, os.W_OK)}")
            except Exception as e:
                print(f"   Could not list {build_java_path}: {e}")

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

    # DEBUG: Check what's actually in /tmp/java
    for v in ["8", "11", "17", "21"]:
        debug_java_paths(v)
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
    all_paths = [JAVA_BASE_PATH] + JAVA_FALLBACK_PATHS + [f"/opt/java-{version}"]
    checked_paths = ", ".join(all_paths)
    
    # Provide more detailed error message
    error_msg = f"❌ No supported Java version found. Checked: {checked_paths}"
    
    # Check if base path exists and list contents
    if os.path.exists(JAVA_BASE_PATH):
        try:
            contents = os.listdir(JAVA_BASE_PATH)
            if contents:
                error_msg += f"\n   Found in {JAVA_BASE_PATH}: {', '.join(contents)}"
            else:
                error_msg += f"\n   {JAVA_BASE_PATH} exists but is empty"
        except Exception as e:
            error_msg += f"\n   Could not list {JAVA_BASE_PATH}: {e}"
    else:
        error_msg += f"\n   {JAVA_BASE_PATH} does not exist (Java may not have been installed during build)"
    
    # Also provide info about RENDER_DISK_PATH if it exists
    render_java_path = os.path.join(RENDER_DISK_PATH, "java")
    if render_java_path != JAVA_BASE_PATH and os.path.exists(RENDER_DISK_PATH):
        error_msg += f"\n   RENDER_DISK_PATH: {RENDER_DISK_PATH} (exists: True, writable: {os.access(RENDER_DISK_PATH, os.W_OK)})"
    
    raise RuntimeError(error_msg)
