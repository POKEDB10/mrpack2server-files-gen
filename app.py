# Standard library imports
import atexit
import csv
import datetime
import io
import json
import logging
import multiprocessing
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from collections import deque, defaultdict
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
from multiprocessing import Process, Queue
from threading import Lock, local

# Third-party imports
import aiohttp
import asyncio
import bcrypt
import portalocker
import psutil
import requests
from flask import (
    Flask, Response, after_this_request, jsonify, make_response,
    redirect, render_template, request, send_file, session, url_for
)
from flask_socketio import SocketIO

# Local imports
from java_resolver import get_java_path, log_installed_java_versions, resolve_java_version


# Constants
MAX_UPLOAD_SIZE = 500 * 1024 * 1024  # 500MB
MAX_FILENAME_LENGTH = 50
CLEANUP_DELAY = 300  # 5 minutes
MAX_RETRIES = 3
LOCK_TIMEOUT = 5
MIN_INSTALLER_SIZE = 1000  # bytes
MIN_DISK_SPACE = 1 * 1024**3  # 1GB
MIN_MEMORY = 512 * 1024**2  # 512MB
DEFAULT_INSTALLER_TIMEOUT = 300  # 5 minutes
NEOFORGE_TIMEOUT = 900  # 15 minutes
NO_OUTPUT_TIMEOUT = 300  # 5 minutes
MAX_WORKERS_DOWNLOAD = 16  # Reduced from 32 for better resource management
MAX_WORKERS_COPY = 8  # Reduced from 16
CHUNK_SIZE = 8192  # 8KB chunks for downloads

# Flask app initialization
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY') or os.urandom(24)
app.config['MAX_CONTENT_LENGTH'] = MAX_UPLOAD_SIZE

# Thread-local storage for request_id tracking (must be defined before WebLogHandler)
_thread_local = local()

# Custom logging handler that also pushes to web interface
class WebLogHandler(logging.Handler):
    """Custom logging handler that pushes logs to web interface when request_id is available."""
    
    def emit(self, record):
        """Emit a log record to both console and web interface."""
        # Get request_id from thread-local storage
        try:
            request_id = getattr(_thread_local, 'request_id', None)
        except (AttributeError, NameError):
            # _thread_local might not be initialized yet during module import
            request_id = None
        
        # Format the log message
        msg = self.format(record)
        
        # If we have a request_id, also push to web interface
        if request_id and isinstance(request_id, str):
            try:
                # Extract just the message part (remove timestamp/levelname if present)
                # The format is usually: "timestamp - levelname - message"
                # We want just the message part for web logs
                if ' - ' in msg:
                    parts = msg.split(' - ', 2)
                    if len(parts) >= 3:
                        web_msg = parts[2]  # Get the actual message
                    else:
                        web_msg = msg
                else:
                    web_msg = msg
                
                # Only push if it's not already a push_log message (avoid duplicates)
                # Use a flag to prevent infinite recursion
                if not web_msg.startswith(f"[{request_id}]"):
                    try:
                        # Check if we're in a push_log call to avoid recursion
                        if hasattr(_thread_local, '_in_push_log'):
                            return
                        _thread_local._in_push_log = True
                        try:
                            # Import push_log from module namespace
                            import app as app_module
                            if hasattr(app_module, 'push_log'):
                                app_module.push_log(request_id, web_msg)
                        finally:
                            if hasattr(_thread_local, '_in_push_log'):
                                delattr(_thread_local, '_in_push_log')
                    except (AttributeError, NameError, Exception):
                        # Silently fail to avoid breaking logging
                        pass
            except Exception:
                # Don't fail if push_log fails
                pass

# Initialize logging
# Check if PRIMARY_WORKER is set, or if we're running locally (not via Gunicorn)
is_primary = os.environ.get("PRIMARY_WORKER") == "1"
is_local = os.environ.get("RUNNING_LOCALLY") == "1" or not os.environ.get("GUNICORN_CMD_ARGS")

if is_primary or is_local:
    # Primary worker or local development: full logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[logging.StreamHandler(), WebLogHandler()]
    )
    logging.info("🛠 Starting Minecraft Server File Generator (MSFG)")
    if is_local:
        logging.info("📍 Running in local development mode")
    log_installed_java_versions()
else:
    # Other workers: minimal logging setup
    logging.basicConfig(
        level=logging.WARNING,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[logging.StreamHandler(), WebLogHandler()]
    )

# Global state - improved for concurrent access
log_buffers = defaultdict(list)
log_locks = defaultdict(Lock)
_log_buffer_creation_lock = Lock()  # Lock for creating new log buffer entries
generated_server_count = 0
generated_server_lock = Lock()
download_executor = ThreadPoolExecutor(max_workers=MAX_WORKERS_DOWNLOAD)
copy_executor = ThreadPoolExecutor(max_workers=MAX_WORKERS_COPY)
active_users = set()
_active_users_lock = Lock()  # Lock for active_users set

# Note: _thread_local is defined earlier (before WebLogHandler) to avoid NameError

# HTTP session with connection pooling for better performance under load
_http_session = None
_http_session_lock = Lock()

def get_http_session():
    """Get or create a shared HTTP session with connection pooling."""
    global _http_session
    if _http_session is None:
        with _http_session_lock:
            if _http_session is None:
                _http_session = requests.Session()
                # Configure connection pooling
                adapter = requests.adapters.HTTPAdapter(
                    pool_connections=50,  # Number of connection pools to cache
                    pool_maxsize=100,      # Max connections per pool
                    max_retries=3,         # Retry failed requests
                    pool_block=False       # Don't block if pool is full
                )
                _http_session.mount('http://', adapter)
                _http_session.mount('https://', adapter)
    return _http_session

# Check for Render disk mount path (for persistent storage)
RENDER_DISK_PATH = os.environ.get("RENDER_DISK_PATH", "/opt/render/project/src/data")

# Forge server file cache (version -> cached file path)
# Use Render disk mount if available
if os.path.exists(RENDER_DISK_PATH) and os.access(RENDER_DISK_PATH, os.W_OK):
    _forge_cache_dir = os.path.join(RENDER_DISK_PATH, "forge_cache")
else:
    _forge_cache_dir = os.path.join(tempfile.gettempdir(), "forge_cache")
os.makedirs(_forge_cache_dir, exist_ok=True)
_forge_cache = {}  # {version: {"path": str, "is_zip": bool, "size": int, "timestamp": float}}
_forge_cache_lock = Lock()
FORGE_CACHE_MAX_AGE = 7 * 24 * 3600  # 7 days
FORGE_CACHE_MAX_SIZE = 10 * 1024**3  # 10GB max cache size

# Quilt installer cache
# Use Render disk mount if available, otherwise use temp directory
if os.path.exists(RENDER_DISK_PATH) and os.access(RENDER_DISK_PATH, os.W_OK):
    _quilt_cache_dir = os.path.join(RENDER_DISK_PATH, "quilt_cache")
else:
    _quilt_cache_dir = os.path.join(tempfile.gettempdir(), "quilt_cache")
os.makedirs(_quilt_cache_dir, exist_ok=True)
_quilt_installer_cache_path = os.path.join(_quilt_cache_dir, "quilt-installer-latest.jar")
_quilt_installer_cache_lock = Lock()
QUILT_INSTALLER_CACHE_MAX_AGE = 1 * 24 * 3600  # 1 day (check for updates daily)
# Use a writable location for the count file (prefer current directory, fallback to temp)
def get_writable_count_file_dir():
    """Get a writable directory for the count file.
    Priority: env var > local project dir (if local) > /tmp > /var/tmp > subdirectory in current dir > system temp
    Note: /tmp is ephemeral and will be lost on container restart, but is always available in Docker.
    For persistence, use COUNT_FILE_DIR env var or mount a volume.
    To sync between local and Docker/Render, set COUNT_FILE_DIR to the same path in both environments.
    NEVER returns /app (protected in Docker)."""
    # 1. Check for user-specified directory via environment variable (highest priority for syncing)
    env_dir = os.environ.get("COUNT_FILE_DIR")
    if env_dir:
        try:
            os.makedirs(env_dir, exist_ok=True)
            test_file = os.path.join(env_dir, ".test_write")
            with open(test_file, 'w') as f:
                f.write('test')
            os.remove(test_file)
            # Never use /app
            if env_dir != '/app' and not env_dir.startswith('/app/'):
                return env_dir
        except (OSError, IOError, PermissionError):
            logging.warning(f"⚠️ COUNT_FILE_DIR '{env_dir}' not writable, trying fallback...")
    
    # 2. For local development, prioritize project directory (persistent and visible)
    is_local = os.environ.get("RUNNING_LOCALLY") == "1" or not os.environ.get("GUNICORN_CMD_ARGS")
    if is_local:
        try:
            # Try project root directory first (where the code is)
            current_dir = os.getcwd()
            # NEVER use /app (protected in Docker)
            if current_dir == '/app' or current_dir.startswith('/app/'):
                raise PermissionError("/app is protected, skipping")
            # Check if we can write to current directory
            test_file = os.path.join(current_dir, ".test_write")
            with open(test_file, 'w') as f:
                f.write('test')
            os.remove(test_file)
            return current_dir
        except (OSError, IOError, PermissionError):
            # If root not writable, try data subdirectory
            try:
                current_dir = os.getcwd()
                # NEVER use /app
                if current_dir == '/app' or current_dir.startswith('/app/'):
                    raise PermissionError("/app is protected, skipping")
                data_dir = os.path.join(current_dir, "data")
                os.makedirs(data_dir, exist_ok=True)
                test_file = os.path.join(data_dir, ".test_write")
                with open(test_file, 'w') as f:
                    f.write('test')
                os.remove(test_file)
                return data_dir
            except (OSError, IOError, PermissionError):
                pass
    
    # 3. Try /tmp FIRST (always exists and writable in Docker containers)
    try:
        # /tmp should always exist, but ensure it does
        if not os.path.exists('/tmp'):
            os.makedirs('/tmp', exist_ok=True)
        # Verify it's actually a directory and writable
        if os.path.isdir('/tmp'):
            test_file = os.path.join('/tmp', ".test_write")
            with open(test_file, 'w') as f:
                f.write('test')
                f.flush()
                os.fsync(f.fileno())  # Ensure write completes
            os.remove(test_file)
            return '/tmp'
    except (OSError, IOError, PermissionError) as e:
        logging.warning(f"⚠️ /tmp not writable: {e}, trying alternatives...")
        pass
    
    # 4. Try /var/tmp (more persistent than /tmp in some systems, but may not exist)
    try:
        # Ensure parent directory exists first
        if not os.path.exists('/var'):
            raise OSError("/var does not exist")
        os.makedirs('/var/tmp', exist_ok=True)
        # Verify it's actually a directory and writable
        if os.path.isdir('/var/tmp'):
            test_file = os.path.join('/var/tmp', ".test_write")
            with open(test_file, 'w') as f:
                f.write('test')
                f.flush()
                os.fsync(f.fileno())
            os.remove(test_file)
            return '/var/tmp'
    except (OSError, IOError, PermissionError) as e:
        logging.warning(f"⚠️ /var/tmp not available: {e}, trying alternatives...")
        pass
    
    # 5. Try to create a subdirectory in current directory (might work even if root is protected)
    try:
        current_dir = os.getcwd()
        # Skip /app
        if current_dir != '/app' and not current_dir.startswith('/app/'):
            data_dir = os.path.join(current_dir, "data")
            os.makedirs(data_dir, exist_ok=True)
            test_file = os.path.join(data_dir, ".test_write")
            with open(test_file, 'w') as f:
                f.write('test')
                f.flush()
                os.fsync(f.fileno())
            os.remove(test_file)
            return data_dir
    except (OSError, IOError, PermissionError):
        pass
    
    # 6. Last resort: system temp directory
    temp_dir = tempfile.gettempdir()
    os.makedirs(temp_dir, exist_ok=True)
    logging.warning(f"⚠️ Using system temp directory for count file (may be ephemeral). Set COUNT_FILE_DIR env var for persistence.")
    return temp_dir

# Use Render disk mount for count file if available
if os.path.exists(RENDER_DISK_PATH) and os.access(RENDER_DISK_PATH, os.W_OK):
    _count_file_dir = RENDER_DISK_PATH
else:
    _count_file_dir = get_writable_count_file_dir()
    # CRITICAL: Never use /app (protected in Docker) - force to /tmp if somehow /app was returned
    if _count_file_dir == '/app' or _count_file_dir.startswith('/app/'):
        logging.warning("⚠️ /app directory detected, forcing to /tmp for count file")
        _count_file_dir = '/tmp'
        os.makedirs(_count_file_dir, exist_ok=True)

# Ensure the directory exists and is writable before setting COUNT_FILE
try:
    # Create directory if it doesn't exist
    if not os.path.exists(_count_file_dir):
        os.makedirs(_count_file_dir, exist_ok=True)
    
    # Verify directory exists
    if not os.path.exists(_count_file_dir):
        raise OSError(f"Directory does not exist: {_count_file_dir}")
    
    # Test write access by creating and deleting a test file
    test_file = os.path.join(_count_file_dir, ".test_write")
    try:
        with open(test_file, 'w') as f:
            f.write('test')
        os.remove(test_file)
    except (OSError, IOError, PermissionError) as test_err:
        # Directory not writable, fallback to a known writable directory
        logging.warning(f"⚠️ Directory {_count_file_dir} not writable ({test_err}), trying fallback...")
        _count_file_dir = get_writable_count_file_dir()
        # Ensure fallback directory exists
        if not os.path.exists(_count_file_dir):
            os.makedirs(_count_file_dir, exist_ok=True)
except (OSError, PermissionError) as dir_err:
    # Can't create directory, fallback to writable directory
    logging.warning(f"⚠️ Could not create directory {_count_file_dir} ({dir_err}), trying fallback...")
    _count_file_dir = get_writable_count_file_dir()
    try:
        if not os.path.exists(_count_file_dir):
            os.makedirs(_count_file_dir, exist_ok=True)
    except (OSError, PermissionError):
        # Last resort: try temp directory (should always exist and be writable)
        _count_file_dir = tempfile.gettempdir()
        os.makedirs(_count_file_dir, exist_ok=True)

COUNT_FILE = os.path.join(_count_file_dir, "generated_server_count.txt")
# CRITICAL: Final safety check - NEVER use /app
if COUNT_FILE.startswith('/app/'):
    logging.warning(f"⚠️ COUNT_FILE was set to /app path ({COUNT_FILE}), forcing to /tmp")
    _count_file_dir = '/tmp'
    os.makedirs(_count_file_dir, exist_ok=True)
    COUNT_FILE = os.path.join(_count_file_dir, "generated_server_count.txt")

# Log the final count file location for debugging
if os.environ.get("PRIMARY_WORKER") == "1":
    logging.info(f"💾 Count file location: {COUNT_FILE}")
# Ensure COUNT_FILE is always a string, never a file object
if not isinstance(COUNT_FILE, str):
    COUNT_FILE = str(COUNT_FILE)
    # If it's a file object representation, reset to default
    if COUNT_FILE.startswith('<') and ('TextIOWrapper' in COUNT_FILE or 'file' in COUNT_FILE.lower()):
        _count_file_dir = get_writable_count_file_dir()
        COUNT_FILE = os.path.join(_count_file_dir, "generated_server_count.txt")
        # Safety check again
        if COUNT_FILE.startswith('/app/'):
            _count_file_dir = '/tmp'
            os.makedirs(_count_file_dir, exist_ok=True)
            COUNT_FILE = os.path.join(_count_file_dir, "generated_server_count.txt")
recent_ips = deque(maxlen=100)
access_log = deque(maxlen=200)
socketio = SocketIO(app, async_mode='gevent')
logging.getLogger('engineio').setLevel(logging.WARNING)
logging.getLogger('socketio').setLevel(logging.WARNING)

# Directories
# Use Render disk mount if available, otherwise use temp directory
if os.path.exists(RENDER_DISK_PATH) and os.access(RENDER_DISK_PATH, os.W_OK):
    PERSISTENT_TEMP_ROOT = os.path.join(RENDER_DISK_PATH, "servers")
else:
    PERSISTENT_TEMP_ROOT = os.path.join(tempfile.gettempdir(), "servers")
os.makedirs(PERSISTENT_TEMP_ROOT, exist_ok=True)
download_status = {}  # {request_id: {"zip_path": str, "ready": bool, "cleanup_scheduled": bool}}
download_status_lock = Lock()  # Lock for download_status dictionary access

# Multiprocessing setup
if platform.system() != 'Windows':
    try:
        multiprocessing.set_start_method('spawn', force=True)
    except RuntimeError:
        pass  # Already set

# Don't initialize count at module level - let initialize_server_count() handle it
# This prevents race conditions and ensures proper initialization order


def initialize_server_count():
    """Initialize the server count from file."""
    global generated_server_count, COUNT_FILE
    try:
        # Ensure COUNT_FILE is a valid string path
        if not isinstance(COUNT_FILE, str):
            COUNT_FILE = str(COUNT_FILE)
            if COUNT_FILE.startswith('<'):
                _count_file_dir = get_writable_count_file_dir()
                COUNT_FILE = os.path.join(_count_file_dir, "generated_server_count.txt")
        
        # Ensure directory exists
        count_dir = os.path.dirname(COUNT_FILE)
        if not count_dir:
            # If no directory in path, use writable directory
            _count_file_dir = get_writable_count_file_dir()
            COUNT_FILE = os.path.join(_count_file_dir, "generated_server_count.txt")
            count_dir = os.path.dirname(COUNT_FILE)
        os.makedirs(count_dir, exist_ok=True)
        
        # Check if the directory is writable
        if not os.access(count_dir, os.W_OK):
            # Try fallback directory
            _count_file_dir = get_writable_count_file_dir()
            COUNT_FILE = os.path.join(_count_file_dir, "generated_server_count.txt")
            count_dir = os.path.dirname(COUNT_FILE)
            if not count_dir:
                count_dir = _count_file_dir
            os.makedirs(count_dir, exist_ok=True)
            if not os.access(count_dir, os.W_OK):
                raise PermissionError(f"No write permissions for directory: {count_dir}")
        
        count_file_path = str(COUNT_FILE)
        
        # Try to read existing count file
        if os.path.exists(count_file_path):
            try:
                with open(count_file_path, "r") as f:
                    try:
                        with portalocker.Lock(f, timeout=LOCK_TIMEOUT):
                            content = f.read().strip()
                            if content:
                                generated_server_count = int(content)
                                # Only log from primary worker to reduce clutter
                                if os.environ.get("PRIMARY_WORKER") == "1":
                                    logging.info(f"✅ Loaded generated server count: {generated_server_count}")
                                return
                    except portalocker.LockException:
                        # File is locked, read without lock as fallback
                        f.seek(0)
                        content = f.read().strip()
                        if content:
                            generated_server_count = int(content)
                            if os.environ.get("PRIMARY_WORKER") == "1":
                                logging.info(f"✅ Loaded generated server count: {generated_server_count} (no lock)")
                            return
            except (ValueError, IOError, OSError) as e:
                # File exists but can't read it, might be corrupt - log and continue to create new
                if os.environ.get("PRIMARY_WORKER") == "1":
                    logging.warning(f"⚠️ Could not read count file, will create new: {e}")
        
        # File doesn't exist or couldn't read it, create/initialize it
        # Only create if it doesn't exist (don't overwrite existing file)
        if not os.path.exists(count_file_path):
            with open(count_file_path, "w") as f:
                try:
                    with portalocker.Lock(f, timeout=LOCK_TIMEOUT):
                        f.write("0")
                        f.flush()
                        os.fsync(f.fileno())  # Ensure data is written to disk
                except portalocker.LockException:
                    # Lock failed, but file was created, try to read it
                    f.seek(0)
                    f.write("0")
                    f.flush()
                    os.fsync(f.fileno())
            generated_server_count = 0
            # Only log from primary worker to reduce clutter
            if os.environ.get("PRIMARY_WORKER") == "1":
                logging.info("✅ Initialized server count file with 0")
        else:
            # File exists but we couldn't read it above, set to 0 as fallback
            generated_server_count = 0
            if os.environ.get("PRIMARY_WORKER") == "1":
                logging.warning("⚠️ Count file exists but couldn't be read, starting with 0")
                
    except PermissionError as e:
        if os.environ.get("PRIMARY_WORKER") == "1":
            logging.error(f"❌ Failed to initialize server count due to permissions: {e}")
        generated_server_count = 0
    except Exception as e:
        if os.environ.get("PRIMARY_WORKER") == "1":
            logging.error(f"❌ Failed to initialize server count: {e}")
        generated_server_count = 0


def increment_generated_server_count():
    """Increment the server count and update the file."""
    global generated_server_count, COUNT_FILE
    max_retries = MAX_RETRIES
    
    # Ensure COUNT_FILE is always a string path, never a file object
    if not isinstance(COUNT_FILE, str):
        COUNT_FILE = str(COUNT_FILE)
        # If it's a file object representation, reset to default
        if COUNT_FILE.startswith('<') and ('TextIOWrapper' in COUNT_FILE or 'file' in COUNT_FILE.lower()):
            _count_file_dir = get_writable_count_file_dir()
            COUNT_FILE = os.path.join(_count_file_dir, "generated_server_count.txt")
    
    for attempt in range(max_retries):
        try:
            # Ensure COUNT_FILE is a string (double-check)
            if not isinstance(COUNT_FILE, str):
                _count_file_dir = get_writable_count_file_dir()
                COUNT_FILE = os.path.join(_count_file_dir, "generated_server_count.txt")
            count_file_path = str(COUNT_FILE)
            
            # Ensure directory exists - handle both absolute and relative paths
            count_dir = os.path.dirname(count_file_path)
            if not count_dir:
                # If no directory in path, use writable directory
                _count_file_dir = get_writable_count_file_dir()
                count_dir = _count_file_dir
                count_file_path = os.path.join(count_dir, "generated_server_count.txt")
                COUNT_FILE = count_file_path
            elif not os.path.isabs(count_dir):
                # If relative path, make it absolute
                count_dir = os.path.abspath(count_dir)
                count_file_path = os.path.join(count_dir, "generated_server_count.txt")
                COUNT_FILE = count_file_path
            
            # CRITICAL: Never use /app directory (protected in Docker)
            if count_dir == '/app' or count_file_path.startswith('/app/'):
                _count_file_dir = get_writable_count_file_dir()
                count_dir = _count_file_dir
                count_file_path = os.path.join(count_dir, "generated_server_count.txt")
                COUNT_FILE = count_file_path
            
            # Create directory if it doesn't exist (create all parent directories too)
            try:
                if count_dir and not os.path.exists(count_dir):
                    os.makedirs(count_dir, exist_ok=True, mode=0o755)
                # Also ensure parent directories exist
                parent = os.path.dirname(count_dir)
                while parent and parent != count_dir and not os.path.exists(parent):
                    try:
                        os.makedirs(parent, exist_ok=True, mode=0o755)
                    except (OSError, PermissionError):
                        break
                    parent = os.path.dirname(parent)
            except (OSError, PermissionError) as dir_create_err:
                # Fallback to writable directory
                logging.warning(f"⚠️ Could not create directory {count_dir}: {dir_create_err}, trying writable directory...")
                _count_file_dir = get_writable_count_file_dir()
                count_dir = _count_file_dir
                count_file_path = os.path.join(count_dir, "generated_server_count.txt")
                COUNT_FILE = count_file_path
                try:
                    os.makedirs(count_dir, exist_ok=True, mode=0o755)
                except (OSError, PermissionError):
                    # Last resort: use temp directory
                    count_dir = tempfile.gettempdir()
                    count_file_path = os.path.join(count_dir, "generated_server_count.txt")
                    COUNT_FILE = count_file_path
                    os.makedirs(count_dir, exist_ok=True, mode=0o755)
            
            # Read current count or initialize to 0
            current_count = 0
            file_exists = os.path.exists(count_file_path)
            
            if file_exists:
                try:
                    with open(count_file_path, 'r') as f:
                        content = f.read().strip()
                        if content:
                            current_count = int(content)
                except (ValueError, IOError, OSError):
                    current_count = 0
            
            # Increment count
            new_count = current_count + 1
            
            # Write new count with file locking
            # Ensure count_file_path is a valid string path before opening
            if not isinstance(count_file_path, str) or count_file_path.startswith('<'):
                # Invalid path, reset to writable directory
                _count_file_dir = get_writable_count_file_dir()
                count_dir = _count_file_dir
                count_file_path = os.path.join(count_dir, "generated_server_count.txt")
                COUNT_FILE = count_file_path
                os.makedirs(count_dir, exist_ok=True)
            
            # Ensure the directory exists and is writable BEFORE trying to open the file
            # This is a critical check - the directory MUST exist and be writable
            try:
                # Double-check: create directory if it doesn't exist (with all parent directories)
                if count_dir:
                    if not os.path.exists(count_dir):
                        os.makedirs(count_dir, exist_ok=True, mode=0o755)
                    # Also ensure all parent directories exist
                    parent = os.path.dirname(count_dir)
                    while parent and parent != count_dir and parent != os.path.dirname(parent):
                        if not os.path.exists(parent):
                            try:
                                os.makedirs(parent, exist_ok=True, mode=0o755)
                            except (OSError, PermissionError):
                                break
                        parent = os.path.dirname(parent)
                
                # Verify directory exists now (critical check)
                if not os.path.exists(count_dir):
                    # If directory doesn't exist, force to /tmp (always available in Docker)
                    logging.warning(f"⚠️ Directory {count_dir} does not exist, forcing to /tmp")
                    count_dir = '/tmp'
                    count_file_path = os.path.join(count_dir, "generated_server_count.txt")
                    COUNT_FILE = count_file_path
                    os.makedirs(count_dir, exist_ok=True)
                
                # Verify it's actually a directory, not a file
                if not os.path.isdir(count_dir):
                    # If not a directory, force to /tmp
                    logging.warning(f"⚠️ Path {count_dir} is not a directory, forcing to /tmp")
                    count_dir = '/tmp'
                    count_file_path = os.path.join(count_dir, "generated_server_count.txt")
                    COUNT_FILE = count_file_path
                    os.makedirs(count_dir, exist_ok=True)
                
                # Test if directory is writable by trying to create a test file
                test_file = os.path.join(count_dir, ".test_write_count")
                try:
                    with open(test_file, 'w') as tf:
                        tf.write('test')
                        tf.flush()
                        os.fsync(tf.fileno())  # Force write to disk
                    os.remove(test_file)
                except (OSError, IOError, PermissionError) as test_err:
                    raise PermissionError(f"Directory not writable: {count_dir} (test failed: {test_err})")
                
            except (OSError, PermissionError) as dir_err:
                # Directory creation/verification failed, try fallback
                if attempt < max_retries - 1:
                    # Try writable directory
                    _count_file_dir = get_writable_count_file_dir()
                    count_dir = _count_file_dir
                    count_file_path = os.path.join(count_dir, "generated_server_count.txt")
                    COUNT_FILE = count_file_path
                    try:
                        os.makedirs(count_dir, exist_ok=True)
                        # Verify it's writable
                        test_file = os.path.join(count_dir, ".test_write_count")
                        with open(test_file, 'w') as tf:
                            tf.write('test')
                        os.remove(test_file)
                    except (OSError, PermissionError):
                        # Try temp directory as last resort
                        count_dir = tempfile.gettempdir()
                        count_file_path = os.path.join(count_dir, "generated_server_count.txt")
                        COUNT_FILE = count_file_path
                        os.makedirs(count_dir, exist_ok=True)
                    time.sleep(0.5)
                    continue
                else:
                    # Last attempt, use temp directory
                    count_dir = tempfile.gettempdir()
                    count_file_path = os.path.join(count_dir, "generated_server_count.txt")
                    COUNT_FILE = count_file_path
                    try:
                        os.makedirs(count_dir, exist_ok=True)
                    except (OSError, PermissionError):
                        pass  # Will still try to write
            
            # Now try to open and write the file
            # Final verification: ensure directory still exists before opening
            if not os.path.exists(count_dir):
                raise OSError(f"Directory disappeared before write: {count_dir}")
            
            try:
                # Use 'w+' mode to ensure file is created/truncated if it exists
                with open(count_file_path, 'w+') as f:
                    try:
                        with portalocker.Lock(f, timeout=LOCK_TIMEOUT):
                            f.seek(0)  # Go to beginning
                            f.truncate()  # Clear any existing content
                            f.write(str(new_count))
                            f.flush()
                            os.fsync(f.fileno())  # Ensure data is written to disk
                            generated_server_count = new_count
                            # Verify the write succeeded by reading it back
                            f.seek(0)
                            written = f.read().strip()
                            if written != str(new_count):
                                raise IOError(f"Write verification failed: wrote {new_count} but file contains {written}")
                    except portalocker.LockException as lock_err:
                        if attempt < max_retries - 1:
                            time.sleep(0.5)
                            continue
                        # If lock fails, still try to write without lock as fallback
                        f.seek(0)
                        f.truncate()
                        f.write(str(new_count))
                        f.flush()
                        os.fsync(f.fileno())
                        generated_server_count = new_count
            except (OSError, IOError) as e:
                # Clean up error message - remove any file object representations and fix double brackets
                error_msg = str(e)
                # Remove file object representations from error message
                import re
                error_msg = re.sub(r'<[^>]+TextIOWrapper[^>]+>', count_file_path, error_msg)
                error_msg = re.sub(r'<[^>]+file[^>]+>', count_file_path, error_msg)
                # Fix double brackets in error message
                error_msg = re.sub(r'^\[\[', '[', error_msg)
                error_msg = re.sub(r'\]\]$', ']', error_msg)
                
                if attempt < max_retries - 1:
                    # Try writable directory as fallback
                    _count_file_dir = get_writable_count_file_dir()
                    count_dir = _count_file_dir
                    count_file_path = os.path.join(count_dir, "generated_server_count.txt")
                    COUNT_FILE = count_file_path
                    # Ensure directory exists before retrying
                    try:
                        os.makedirs(count_dir, exist_ok=True)
                    except (OSError, PermissionError):
                        pass  # Will try again
                    time.sleep(0.5)
                    continue
                # Last attempt failed, but still update in-memory count
                generated_server_count = new_count
                if os.environ.get("PRIMARY_WORKER") == "1" or os.environ.get("RUNNING_LOCALLY") == "1":
                    logging.warning(f"⚠️ Failed to write count file, but count incremented in memory: {error_msg}")
                return new_count
            
            # Success
            if os.environ.get("PRIMARY_WORKER") == "1":
                logging.info(f"✅ Incremented server count to: {new_count}")
            return new_count
            
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(0.5)
                continue
            # Last attempt - increment in memory even if file write fails
            generated_server_count += 1
            if os.environ.get("PRIMARY_WORKER") == "1":
                logging.error(f"❌ Max retries reached for incrementing server count: {e}")
            return generated_server_count

def save_server_count():
    """Save the current server count to file on shutdown, non-blocking attempt."""
    global COUNT_FILE
    try:
        # Ensure COUNT_FILE is always a string path, never a file object
        if not isinstance(COUNT_FILE, str):
            COUNT_FILE = str(COUNT_FILE)
        
        # Ensure directory exists
        count_file_path = str(COUNT_FILE)
        count_dir = os.path.dirname(count_file_path)
        if not count_dir:
            # If no directory in path, use writable directory
            count_dir = get_writable_count_file_dir()
            count_file_path = os.path.join(count_dir, "generated_server_count.txt")
            COUNT_FILE = count_file_path
        os.makedirs(count_dir, exist_ok=True)
        
        # Check if the directory is writable
        if not os.access(count_dir, os.W_OK):
            # Try writable directory as fallback
            count_dir = get_writable_count_file_dir()
            count_file_path = os.path.join(count_dir, "generated_server_count.txt")
            COUNT_FILE = count_file_path
            os.makedirs(count_dir, exist_ok=True)
            if not os.access(count_dir, os.W_OK):
                raise PermissionError(f"No write permissions for directory: {count_dir}")
        
        # Ensure COUNT_FILE is still a string path (double-check)
        if not isinstance(COUNT_FILE, str):
            count_file_path = os.path.join(count_dir, "generated_server_count.txt")
            COUNT_FILE = count_file_path
        
        # Try to acquire the lock non-blocking
        with open(count_file_path, "w") as f:
            try:
                portalocker.lock(f, portalocker.LOCK_EX | portalocker.LOCK_NB)
                f.write(str(generated_server_count))
                f.flush()
                os.fsync(f.fileno())  # Ensure data is written to disk
                portalocker.unlock(f)
                # Only log from primary worker to reduce clutter
                if os.environ.get("PRIMARY_WORKER") == "1":
                    logging.info(f"✅ Saved generated server count on exit: {generated_server_count}")
            except portalocker.LockException:
                # Another process is already saving, that's fine
                pass
    except PermissionError as e:
        if os.environ.get("PRIMARY_WORKER") == "1":
            logging.error(f"❌ Failed to save server count due to permissions: {e}")
    except Exception as e:
        if os.environ.get("PRIMARY_WORKER") == "1":
            # Ensure COUNT_FILE is a string for error message
            count_file_str = str(COUNT_FILE) if isinstance(COUNT_FILE, str) else "unknown"
            logging.error(f"❌ Failed to save server count on exit: {e} (file: {count_file_str})")


# Also add signal handlers for graceful shutdown
def handle_shutdown(signum, frame):
    logging.info(f"Received signal {signum}, shutting down gracefully...")
    save_server_count()
    _flush_admin_logs_to_file()
    sys.exit(0)

# Register signal handlers only if not in main (to avoid conflicts)
# In main block, we'll register enhanced handlers
if __name__ != '__main__':
    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)
atexit.register(save_server_count)


# Admin system configuration
USERS_FILE = os.path.join(os.getcwd(), "config", "users.json")
ADMIN_LOG_FILE = os.path.join(os.getcwd(), "config", "admin_actions.json")
SESSION_TIMEOUT = 3600  # 1 hour in seconds
MAX_LOGIN_ATTEMPTS = 5
LOGIN_LOCKOUT_TIME = 300  # 5 minutes in seconds

os.makedirs(os.path.dirname(USERS_FILE), exist_ok=True)

# Login attempt tracking (IP -> {attempts: int, lockout_until: float})
login_attempts = defaultdict(lambda: {"attempts": 0, "lockout_until": 0})
login_attempts_lock = Lock()

# Admin action logging - improved for concurrent access
admin_actions = deque(maxlen=2000)  # Increased for better history
admin_actions_lock = Lock()
_admin_log_file_lock = Lock()  # Separate lock for file operations
MAX_ADMIN_LOG_ENTRIES = 5000  # Max entries in file
ADMIN_LOG_BATCH_SIZE = 10  # Write to file every N entries
_admin_log_pending_count = 0  # Track pending writes

def log_admin_action(username, action, details=None, ip=None):
    """Log admin actions for audit trail with thread-safe file operations."""
    global _admin_log_pending_count
    
    # Try to get IP from request context if not provided
    if ip is None:
        try:
            if hasattr(request, 'remote_addr'):
                ip = request.remote_addr
            else:
                ip = "unknown"
        except (RuntimeError, AttributeError):
            ip = "unknown"
    
    # Get user agent for better logging
    user_agent = "unknown"
    try:
        if hasattr(request, 'headers'):
            user_agent = request.headers.get('User-Agent', 'unknown')[:200]  # Limit length
    except (RuntimeError, AttributeError):
        pass
    
    # Get request path if available
    request_path = "unknown"
    try:
        if hasattr(request, 'path'):
            request_path = request.path
    except (RuntimeError, AttributeError):
        pass
    
    # Format details better
    formatted_details = details or {}
    if isinstance(formatted_details, dict):
        # Ensure details are JSON-serializable
        formatted_details = {k: str(v) if not isinstance(v, (str, int, float, bool, type(None))) else v 
                            for k, v in formatted_details.items()}
    
    # Create more detailed entry
    entry = {
        "username": username,
        "action": action,
        "details": formatted_details,
        "ip": ip,
        "user_agent": user_agent,
        "path": request_path,
        "timestamp": datetime.datetime.now().isoformat(),
        "id": str(uuid.uuid4()),  # Unique ID for each entry
        "severity": "high" if action in ["login", "logout", "add_user", "delete_user", "change_password", "failed_login"] else "normal"
    }
    
    # Add to in-memory deque (thread-safe append)
    with admin_actions_lock:
        admin_actions.appendleft(entry)
    
    # Batch file writes to reduce I/O under concurrent load
    with _admin_log_file_lock:
        _admin_log_pending_count += 1
        should_write = _admin_log_pending_count >= ADMIN_LOG_BATCH_SIZE
    
    # Write to file in batches or immediately if critical actions
    critical_actions = ["login", "logout", "add_user", "delete_user", "change_password"]
    if should_write or action in critical_actions:
        _flush_admin_logs_to_file()
        with _admin_log_file_lock:
            _admin_log_pending_count = 0

def _flush_admin_logs_to_file():
    """Thread-safe function to flush admin logs to file."""
    try:
        # Ensure directory exists
        os.makedirs(os.path.dirname(ADMIN_LOG_FILE), exist_ok=True)
        
        # Get current in-memory logs
        with admin_actions_lock:
            current_actions = list(admin_actions)
        
        # Use atomic write pattern: write to temp file, then rename
        temp_file = ADMIN_LOG_FILE + ".tmp"
        
        # Read existing file if it exists
        existing_actions = []
        if os.path.exists(ADMIN_LOG_FILE):
            try:
                with open(ADMIN_LOG_FILE, "r") as f:
                    with portalocker.Lock(f, timeout=2):
                        try:
                            existing_actions = json.load(f)
                        except (json.JSONDecodeError, ValueError):
                            existing_actions = []
            except (IOError, OSError, portalocker.LockException):
                # If we can't read, start fresh
                existing_actions = []
        
        # Merge: prioritize in-memory (newer) entries, avoid duplicates
        existing_ids = {e.get("id") for e in existing_actions if e.get("id")}
        merged = []
        
        # Add in-memory entries first (they're newer)
        for entry in current_actions:
            if entry.get("id") not in existing_ids:
                merged.append(entry)
                existing_ids.add(entry.get("id"))
        
        # Add existing entries that aren't in memory
        for entry in existing_actions:
            if entry.get("id") not in existing_ids:
                merged.append(entry)
                existing_ids.add(entry.get("id"))
        
        # Sort by timestamp (newest first) and limit
        merged.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
        merged = merged[:MAX_ADMIN_LOG_ENTRIES]
        
        # Atomic write: write to temp file, then rename
        with open(temp_file, "w") as f:
            json.dump(merged, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        
        # Atomic rename (works on most filesystems)
        try:
            if os.path.exists(ADMIN_LOG_FILE):
                os.remove(ADMIN_LOG_FILE)
            os.rename(temp_file, ADMIN_LOG_FILE)
        except (OSError, IOError):
            # Fallback: try to remove temp file
            try:
                if os.path.exists(temp_file):
                    os.remove(temp_file)
            except:
                pass
            raise
        
    except Exception as e:
        logging.warning(f"Failed to flush admin action log to file: {e}")

def _load_admin_logs_from_file():
    """Load admin logs from file into memory (called on startup)."""
    try:
        if not os.path.exists(ADMIN_LOG_FILE):
            return
        
        with open(ADMIN_LOG_FILE, "r") as f:
            try:
                with portalocker.Lock(f, timeout=2):
                    actions = json.load(f)
                    # Load into memory (newest first)
                    with admin_actions_lock:
                        admin_actions.clear()
                        for entry in reversed(actions[:2000]):  # Load last 2000
                            admin_actions.append(entry)
            except (json.JSONDecodeError, ValueError, portalocker.LockException):
                logging.warning("Failed to load admin logs from file")
    except Exception as e:
        logging.warning(f"Error loading admin logs: {e}")

def load_admin_users():
    """Load admin users from file, reloading each time for dynamic updates."""
    try:
        if not os.path.exists(USERS_FILE):
            # Create default users file if it doesn't exist
            default_users = {
                "admin": bcrypt.hashpw(b"admin123", bcrypt.gensalt()).decode()
            }
            with open(USERS_FILE, "w") as f:
                json.dump(default_users, f, indent=2)
            os.chmod(USERS_FILE, 0o600)
            logging.warning("⚠️ Created default admin user: admin/admin123 - PLEASE CHANGE THIS!")
            return {k: v.encode() if isinstance(v, str) else v for k, v in default_users.items()}
        
        with open(USERS_FILE, "r") as f:
            users = json.load(f)
        return {k: v.encode() if isinstance(v, str) else v for k, v in users.items()}  
    except Exception as e:
        logging.error(f"Failed to load admin users: {e}")
        return {}

def save_admin_users(users_dict):
    """Save admin users to file."""
    try:
        # Convert bytes to strings for JSON serialization
        users_to_save = {
            k: v.decode() if isinstance(v, bytes) else v 
            for k, v in users_dict.items()
        }
        with open(USERS_FILE, "w") as f:
            json.dump(users_to_save, f, indent=2)
        os.chmod(USERS_FILE, 0o600)
        return True
    except Exception as e:
        logging.error(f"Failed to save admin users: {e}")
        return False

def check_login_lockout(ip):
    """Check if IP is locked out from login attempts."""
    with login_attempts_lock:
        attempts_info = login_attempts[ip]
        if attempts_info["lockout_until"] > time.time():
            remaining = int(attempts_info["lockout_until"] - time.time())
            return True, remaining
        return False, 0

def record_login_attempt(ip, success):
    """Record a login attempt (success or failure)."""
    with login_attempts_lock:
        if success:
            # Reset on success
            login_attempts[ip] = {"attempts": 0, "lockout_until": 0}
        else:
            attempts_info = login_attempts[ip]
            attempts_info["attempts"] += 1
            if attempts_info["attempts"] >= MAX_LOGIN_ATTEMPTS:
                attempts_info["lockout_until"] = time.time() + LOGIN_LOCKOUT_TIME
                logging.warning(f"🔒 IP {ip} locked out for {LOGIN_LOCKOUT_TIME} seconds after {MAX_LOGIN_ATTEMPTS} failed attempts")

def require_admin(view_func):
    """Decorator to require admin authentication with session timeout check."""
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin_login", next=request.path))
        
        # Check session timeout
        last_activity = session.get("last_activity")
        if last_activity:
            elapsed = time.time() - last_activity
            if elapsed > SESSION_TIMEOUT:
                session.clear()
                return redirect(url_for("admin_login", next=request.path, expired=1))
        
        # Update last activity
        session["last_activity"] = time.time()
        return view_func(*args, **kwargs)
    return wrapped


@app.before_request
def track_active_users():
    """Track active users and access logs (thread-safe)."""
    try:
        ip = request.remote_addr
        session['ip'] = ip
        
        # Thread-safe active users tracking
        with _active_users_lock:
            active_users.add(ip)
        
        entry = {
            "ip": ip,
            "path": request.path,
            "time": datetime.datetime.now().isoformat()
        }
        
        # Thread-safe deque operations (appendleft is atomic for single operations)
        recent_ips.appendleft(entry)
        access_log.appendleft(entry)
    except Exception as e:
        # Don't fail the request if tracking fails
        logging.warning(f"Error tracking active user: {e}")


def delayed_cleanup(zip_path, server_dir, request_id, delay=CLEANUP_DELAY):
    """Clean up temporary files and directories after delay."""
    time.sleep(delay)
    try:
        # Validate paths to prevent accidental deletion
        if zip_path and os.path.exists(zip_path):
            # Ensure zip_path is in temp directory
            if tempfile.gettempdir() in os.path.abspath(zip_path):
                os.remove(zip_path)
        
        if server_dir and os.path.exists(server_dir):
            # Ensure server_dir is in our temp root
            if PERSISTENT_TEMP_ROOT in os.path.abspath(server_dir):
                shutil.rmtree(server_dir, ignore_errors=True)
        
        logging.info(f"[{request_id}] 🧹 Cleanup completed for {server_dir}")
        
        # Clean up request tracking (thread-safe)
        with _log_buffer_creation_lock:
            if request_id in log_locks:
                with download_status_lock:
                    if request_id in download_status:
                        del download_status[request_id]
                try:
                    with log_locks[request_id]:
                        if request_id in log_buffers:
                            del log_buffers[request_id]
                except Exception:
                    pass  # Lock might already be deleted
                # Remove lock last
                try:
                    del log_locks[request_id]
                except KeyError:
                    pass  # Already deleted
    except Exception as e:
        logging.warning(f"[{request_id}] ⚠️ Cleanup failed: {e}")

def cleanup_old_log_buffers():
    """Periodically clean up old log buffers to prevent memory leaks."""
    try:
        current_time = time.time()
        max_age = 3600  # 1 hour
        
        with _log_buffer_creation_lock:
            # Get list of request_ids to check
            request_ids = list(log_locks.keys())
            
            for request_id in request_ids:
                try:
                    # Check if buffer is old (no activity for max_age seconds)
                    # This is a simple heuristic - in production you might track last access time
                    with log_locks[request_id]:
                        if request_id in log_buffers:
                            buffer = log_buffers[request_id]
                            # If buffer is empty and old, clean it up
                            if len(buffer) == 0:
                                # Check if request is in download_status (still active)
                                with download_status_lock:
                                    if request_id not in download_status:
                                        # Safe to remove
                                        del log_buffers[request_id]
                                        del log_locks[request_id]
                except (KeyError, Exception):
                    # Already cleaned up or error, continue
                    continue
    except Exception as e:
        logging.warning(f"Error in cleanup_old_log_buffers: {e}")

# Start background task for log buffer cleanup (every 30 minutes)
def start_log_cleanup_task():
    """Start background task to clean up old log buffers."""
    def cleanup_loop():
        while True:
            try:
                socketio.sleep(1800)  # 30 minutes
                cleanup_old_log_buffers()
            except Exception as e:
                logging.warning(f"Error in log cleanup loop: {e}")
                socketio.sleep(300)  # Wait 5 minutes on error
    
    socketio.start_background_task(cleanup_loop)
        
        
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if session.get("is_admin"):
        return redirect(url_for("admin_dashboard"))
    
    error = None
    expired = request.args.get("expired")
    if expired:
        error = "Your session has expired. Please login again."
    
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        ip = request.remote_addr
        
        # Check for lockout
        is_locked, remaining = check_login_lockout(ip)
        if is_locked:
            error = f"Too many failed login attempts. Please try again in {remaining} seconds."
            logging.warning(f"🔒 Login attempt from locked out IP: {ip}")
            return render_template("admin_login.html", error=error)
        
        if not username or not password:
            record_login_attempt(ip, False)
            error = "Username and password are required"
            return render_template("admin_login.html", error=error)
        
        # Load users dynamically (in case they were updated)
        admin_users = load_admin_users()
        hashed = admin_users.get(username)
        
        if hashed and bcrypt.checkpw(password.encode(), hashed):
            session["is_admin"] = True
            session["admin_user"] = username
            session["last_activity"] = time.time()
            session.permanent = True
            record_login_attempt(ip, True)
            log_admin_action(username, "login", {"ip": ip}, ip)
            logging.info(f"✅ Admin login successful: username={username}, ip={ip}")
            
            # Redirect to next page if specified
            next_page = request.args.get("next") or url_for("admin_dashboard")
            return redirect(next_page)
        else:
            record_login_attempt(ip, False)
            log_admin_action("unknown", "failed_login", {"username": username, "ip": ip}, ip)
            logging.warning(f"❌ Admin login failed: username={username}, ip={ip}")
            error = "Invalid username or password"
    
    return render_template("admin_login.html", error=error)


@app.route("/admin/logout")
@require_admin
def admin_logout():
    username = session.get("admin_user", "unknown")
    ip = request.remote_addr
    log_admin_action(username, "logout", {"ip": ip}, ip)
    session.clear()
    return redirect(url_for("admin_login"))


@app.route("/admin/logs")
@require_admin
def view_logs():
    """API endpoint to get access logs with pagination and filtering."""
    try:
        # Get query parameters
        page = int(request.args.get("page", 1))
        per_page = int(request.args.get("per_page", 50))
        ip_filter = request.args.get("ip", "").strip().lower()
        path_filter = request.args.get("path", "").strip().lower()
        search = request.args.get("search", "").strip().lower()
        
        # Validate pagination
        page = max(1, page)
        per_page = min(max(1, per_page), 200)  # Max 200 per page
        
        # Get unique logs (thread-safe copy)
        seen = set()
        unique_logs = []
        with generated_server_lock:  # Use existing lock for access_log
            # Create a snapshot to avoid iteration issues
            log_snapshot = list(access_log)
        
        for entry in log_snapshot:
            key = (entry.get("ip", ""), entry.get("path", ""), entry.get("time", ""))
            if key not in seen:
                seen.add(key)
                unique_logs.append(entry)
        
        # Apply filters
        filtered_logs = []
        for entry in unique_logs:
            entry_ip = entry.get("ip", "").lower()
            entry_path = entry.get("path", "").lower()
            entry_time = entry.get("time", "").lower()
            
            # IP filter
            if ip_filter and ip_filter not in entry_ip:
                continue
            
            # Path filter
            if path_filter and path_filter not in entry_path:
                continue
            
            # Search filter (searches across all fields)
            if search:
                search_text = f"{entry_ip} {entry_path} {entry_time}".lower()
                if search not in search_text:
                    continue
            
            filtered_logs.append(entry)
        
        # Sort by timestamp (newest first)
        filtered_logs.sort(key=lambda x: x.get("time", ""), reverse=True)
        
        # Paginate
        total = len(filtered_logs)
        total_pages = (total + per_page - 1) // per_page
        page = min(page, total_pages) if total_pages > 0 else 1
        start = (page - 1) * per_page
        end = start + per_page
        paginated_logs = filtered_logs[start:end]
        
        return jsonify({
            "access_log": paginated_logs,
            "pagination": {
                "page": page,
                "per_page": per_page,
                "total": total,
                "total_pages": total_pages
            },
            "recent_ips": list(recent_ips)[:50]  # Limit to 50 most recent
        })
    except Exception as e:
        logging.error(f"Error in view_logs: {e}")
        return jsonify({"error": "Failed to retrieve logs", "access_log": [], "pagination": {}}), 500

@app.route("/admin/actions")
@require_admin
def view_admin_actions():
    """API endpoint to get admin action logs with pagination and filtering."""
    try:
        # Get query parameters
        page = int(request.args.get("page", 1))
        per_page = int(request.args.get("per_page", 50))
        username_filter = request.args.get("username", "").strip().lower()
        action_filter = request.args.get("action", "").strip().lower()
        ip_filter = request.args.get("ip", "").strip().lower()
        search = request.args.get("search", "").strip().lower()
        start_date = request.args.get("start_date", "").strip()
        end_date = request.args.get("end_date", "").strip()
        
        # Validate pagination
        page = max(1, page)
        per_page = min(max(1, per_page), 200)  # Max 200 per page
        
        # Get actions (thread-safe copy)
        with admin_actions_lock:
            actions = list(admin_actions)
        
        # Also load from file if needed (for older entries)
        try:
            if os.path.exists(ADMIN_LOG_FILE):
                with open(ADMIN_LOG_FILE, "r") as f:
                    try:
                        with portalocker.Lock(f, timeout=1):
                            file_actions = json.load(f)
                            # Merge with in-memory (avoid duplicates by ID)
                            memory_ids = {a.get("id") for a in actions if a.get("id")}
                            for entry in file_actions:
                                if entry.get("id") not in memory_ids:
                                    actions.append(entry)
                    except (json.JSONDecodeError, portalocker.LockException):
                        pass
        except Exception:
            pass  # Continue with in-memory only
        
        # Apply filters
        filtered_actions = []
        for entry in actions:
            entry_username = entry.get("username", "").lower()
            entry_action = entry.get("action", "").lower()
            entry_ip = entry.get("ip", "").lower()
            entry_timestamp = entry.get("timestamp", "")
            entry_details = str(entry.get("details", {})).lower()
            
            # Username filter
            if username_filter and username_filter not in entry_username:
                continue
            
            # Action filter
            if action_filter and action_filter not in entry_action:
                continue
            
            # IP filter
            if ip_filter and ip_filter not in entry_ip:
                continue
            
            # Date range filter
            if start_date or end_date:
                try:
                    entry_dt = datetime.datetime.fromisoformat(entry_timestamp.replace("Z", "+00:00"))
                    if start_date:
                        start_dt = datetime.datetime.fromisoformat(start_date)
                        if entry_dt < start_dt:
                            continue
                    if end_date:
                        end_dt = datetime.datetime.fromisoformat(end_date)
                        if entry_dt > end_dt:
                            continue
                except (ValueError, TypeError):
                    pass  # Skip date filtering if invalid
            
            # Search filter (searches across all fields including new ones)
            if search:
                entry_user_agent = entry.get("user_agent", "").lower()
                entry_path = entry.get("path", "").lower()
                entry_severity = entry.get("severity", "normal").lower()
                search_text = f"{entry_username} {entry_action} {entry_ip} {entry_timestamp} {entry_details} {entry_user_agent} {entry_path} {entry_severity}".lower()
                if search not in search_text:
                    continue
            
            filtered_actions.append(entry)
        
        # Sort by timestamp (newest first)
        filtered_actions.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
        
        # Paginate
        total = len(filtered_actions)
        total_pages = (total + per_page - 1) // per_page
        page = min(page, total_pages) if total_pages > 0 else 1
        start = (page - 1) * per_page
        end = start + per_page
        paginated_actions = filtered_actions[start:end]
        
        return jsonify({
            "actions": paginated_actions,
            "pagination": {
                "page": page,
                "per_page": per_page,
                "total": total,
                "total_pages": total_pages
            }
        })
    except Exception as e:
        logging.error(f"Error in view_admin_actions: {e}")
        return jsonify({"error": "Failed to retrieve admin actions", "actions": [], "pagination": {}}), 500


@app.route("/admin/logs/view")
@require_admin
def admin_logs_view():
    return render_template("admin_logs.html")

@app.route("/admin/logs/export")
@require_admin
def export_logs_csv():
    """Export access logs to CSV with filtering support."""
    try:
        # Get filters from query params
        ip_filter = request.args.get("ip", "").strip().lower()
        path_filter = request.args.get("path", "").strip().lower()
        search = request.args.get("search", "").strip().lower()
        
        si = io.StringIO()
        writer = csv.writer(si)
        writer.writerow(["IP Address", "Path", "Timestamp"])
        
        # Get unique logs (thread-safe)
        seen = set()
        with generated_server_lock:
            log_snapshot = list(access_log)
        
        for entry in log_snapshot:
            key = (entry.get("ip", ""), entry.get("path", ""), entry.get("time", ""))
            if key not in seen:
                seen.add(key)
                
                # Apply filters
                entry_ip = entry.get("ip", "").lower()
                entry_path = entry.get("path", "").lower()
                entry_time = entry.get("time", "").lower()
                
                if ip_filter and ip_filter not in entry_ip:
                    continue
                if path_filter and path_filter not in entry_path:
                    continue
                if search:
                    search_text = f"{entry_ip} {entry_path} {entry_time}".lower()
                    if search not in search_text:
                        continue
                
                writer.writerow([entry.get("ip", ""), entry.get("path", ""), entry.get("time", "")])
        
        output = make_response(si.getvalue())
        output.headers["Content-Disposition"] = "attachment; filename=access_logs.csv"
        output.headers["Content-type"] = "text/csv"
        return output
    except Exception as e:
        logging.error(f"Error exporting logs: {e}")
        return jsonify({"error": "Failed to export logs"}), 500

@app.route("/admin/actions/export")
@require_admin
def export_admin_actions_csv():
    """Export admin action logs to CSV with filtering support."""
    try:
        # Get filters from query params
        username_filter = request.args.get("username", "").strip().lower()
        action_filter = request.args.get("action", "").strip().lower()
        ip_filter = request.args.get("ip", "").strip().lower()
        search = request.args.get("search", "").strip().lower()
        
        si = io.StringIO()
        writer = csv.writer(si)
        writer.writerow(["Timestamp", "Username", "Action", "IP Address", "User Agent", "Path", "Severity", "Details"])
        
        # Get actions (thread-safe)
        with admin_actions_lock:
            actions = list(admin_actions)
        
        # Also load from file
        try:
            if os.path.exists(ADMIN_LOG_FILE):
                with open(ADMIN_LOG_FILE, "r") as f:
                    try:
                        with portalocker.Lock(f, timeout=1):
                            file_actions = json.load(f)
                            memory_ids = {a.get("id") for a in actions if a.get("id")}
                            for entry in file_actions:
                                if entry.get("id") not in memory_ids:
                                    actions.append(entry)
                    except (json.JSONDecodeError, portalocker.LockException):
                        pass
        except Exception:
            pass
        
        for entry in actions:
            entry_username = entry.get("username", "").lower()
            entry_action = entry.get("action", "").lower()
            entry_ip = entry.get("ip", "").lower()
            entry_details = str(entry.get("details", {}))
            entry_user_agent = entry.get("user_agent", "unknown")
            entry_path = entry.get("path", "unknown")
            entry_severity = entry.get("severity", "normal")
            
            # Apply filters
            if username_filter and username_filter not in entry_username:
                continue
            if action_filter and action_filter not in entry_action:
                continue
            if ip_filter and ip_filter not in entry_ip:
                continue
            if search:
                search_text = f"{entry_username} {entry_action} {entry_ip} {entry_details} {entry_user_agent} {entry_path} {entry_severity}".lower()
                if search not in search_text:
                    continue
            
            writer.writerow([
                entry.get("timestamp", ""),
                entry.get("username", ""),
                entry.get("action", ""),
                entry.get("ip", ""),
                entry_user_agent,
                entry_path,
                entry_severity,
                entry_details
            ])
        
        output = make_response(si.getvalue())
        output.headers["Content-Disposition"] = "attachment; filename=admin_actions.csv"
        output.headers["Content-type"] = "text/csv"
        return output
    except Exception as e:
        logging.error(f"Error exporting admin actions: {e}")
        return jsonify({"error": "Failed to export admin actions"}), 500
    

async def async_download_to_file(session, url, dest_path, request_id, max_size=MAX_UPLOAD_SIZE):
    """Download file asynchronously with size validation."""
    # Set request_id in thread-local storage so WebLogHandler can push logs to web interface
    if request_id:
        _thread_local.request_id = request_id
    try:
        push_log(request_id, f"🌐 Requesting: {url}")
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            push_log(request_id, f"⬇️ Started downloading: {url}")
            resp.raise_for_status()
            
            # Check content length if available
            content_length = resp.headers.get('Content-Length')
            if content_length and int(content_length) > max_size:
                raise ValueError(f"File too large: {content_length} bytes (max: {max_size})")
            
            downloaded = 0
            with open(dest_path, 'wb') as f:
                async for chunk in resp.content.iter_chunked(CHUNK_SIZE):
                    downloaded += len(chunk)
                    if downloaded > max_size:
                        os.remove(dest_path)
                        raise ValueError(f"File exceeds maximum size: {max_size} bytes")
                    f.write(chunk)
        
        push_log(request_id, f"✅ Saved to: {dest_path}")
    except Exception as e:
        # Clean up partial file on error
        if os.path.exists(dest_path):
            try:
                os.remove(dest_path)
            except OSError:
                pass
        push_log(request_id, f"❌ Error downloading {url}: {e}")
        raise


async def parallel_download_and_copy_async(index_data, extract_path, server_dir, request_id):
    """Download mods and copy overrides asynchronously with validation."""
    # Set request_id in thread-local storage so WebLogHandler can push logs to web interface
    # Note: This works because asyncio.run() runs in the same thread
    if request_id:
        _thread_local.request_id = request_id
    overrides_dir = os.path.join(extract_path, "overrides")
    mods_dst_dir = os.path.join(server_dir, "mods")
    config_dst_dir = os.path.join(server_dir, "config")

    os.makedirs(mods_dst_dir, exist_ok=True)
    os.makedirs(config_dst_dir, exist_ok=True)
    
    # Download mods from URLs
    async with aiohttp.ClientSession() as session:
        tasks = []
        files = index_data.get('files', [])
        push_log(request_id, f"📦 Starting download of {len(files)} mods")
        
        for file_info in files:
            # Check if file has downloads
            if 'downloads' not in file_info or not file_info['downloads']:
                continue
            
            url = file_info['downloads'][0]
            # Validate URL
            if not url or not isinstance(url, str) or not url.startswith(('http://', 'https://')):
                push_log(request_id, f"⚠️ Skipping invalid URL: {url}")
                continue
            
            # Get path from fileInfo (Modrinth format)
            path = file_info.get('path', '')
            
            # Only download files that are in mods directory
            if not path or not path.startswith('mods/'):
                continue  # Skip client-only files (config, shaderpacks, resourcepacks, etc.)
            
            # Extract filename from path (e.g., "mods/example-mod.jar" -> "example-mod.jar")
            filename = os.path.basename(path)
            if not filename.endswith('.jar'):
                continue  # Skip non-JAR files
            
            # Sanitize filename
            filename = re.sub(r'[^a-zA-Z0-9._-]', '_', filename)
            if not filename.endswith('.jar'):
                filename += '.jar'
            
            dest_path = os.path.join(mods_dst_dir, filename)
            
            # Skip if already exists
            if os.path.exists(dest_path):
                push_log(request_id, f"⏭️ Skipping {filename} (already exists)")
                continue
            
            tasks.append(async_download_to_file(session, url, dest_path, request_id))
        
        # Download all mods in parallel
        if tasks:
            push_log(request_id, f"⬇️ Downloading {len(tasks)} mods in parallel")
            results = await asyncio.gather(*tasks, return_exceptions=True)
            failed = sum(1 for r in results if isinstance(r, Exception))
            if failed:
                push_log(request_id, f"⚠️ {failed} mod downloads failed")
            push_log(request_id, f"✅ Downloaded {len(tasks) - failed} mods successfully")
    
    def copy_tree(src, dst, label, file_filter=None):
        """Copy directory tree with filtering."""
        folder_log = defaultdict(int)
        if os.path.exists(src):
            for root, _, files in os.walk(src):
                rel_path = os.path.relpath(root, src)
                dest_dir = os.path.join(dst, rel_path)
                os.makedirs(dest_dir, exist_ok=True)
                for file in files:
                    if file_filter and not file_filter(file):
                        continue
                    src_file = os.path.join(root, file)
                    dst_file = os.path.join(dest_dir, file)
                    try:
                        shutil.copy2(src_file, dst_file)
                        folder_log[rel_path] += 1
                        push_log(request_id, f"Copied {label}: {file} to {rel_path}")
                    except (OSError, IOError) as e:
                        push_log(request_id, f"⚠️ Failed to copy {file}: {e}")
            for subfolder, count in folder_log.items():
                push_log(request_id, f"📁 {label}s in '{subfolder}': {count}")
            push_log(request_id, f"✅ Total {label}s copied: {sum(folder_log.values())}")
        else:
            push_log(request_id, f"⚠️ No {label} directory found at {src}")

    await asyncio.get_event_loop().run_in_executor(
        copy_executor, copy_tree,
        os.path.join(overrides_dir, "mods"), mods_dst_dir, "mod",
        lambda f: f.endswith(".jar")
    )
    await asyncio.get_event_loop().run_in_executor(
        copy_executor, copy_tree,
        os.path.join(overrides_dir, "config"), config_dst_dir, "config"
    )

    def copy_all_overrides():
        """Copy all remaining override files."""
        folder_log = defaultdict(int)
        if not os.path.exists(overrides_dir):
            return
        for root, _, files in os.walk(overrides_dir):
            rel_path = os.path.relpath(root, overrides_dir)
            if rel_path.split(os.sep)[0] in {"mods", "config"}:
                continue
            dest_dir = os.path.join(server_dir, rel_path)
            os.makedirs(dest_dir, exist_ok=True)
            for file in files:
                src_file = os.path.join(root, file)
                dst_file = os.path.join(dest_dir, file)
                try:
                    shutil.copy2(src_file, dst_file)
                    folder_log[rel_path] += 1
                    push_log(request_id, f"Copied override: {file} to {rel_path}")
                except (OSError, IOError) as e:
                    push_log(request_id, f"⚠️ Failed to copy override {file}: {e}")
        for subfolder, count in folder_log.items():
            push_log(request_id, f"📁 overrides in '{subfolder}': {count}")
        push_log(request_id, f"✅ Total remaining overrides copied: {sum(folder_log.values())}")

    await asyncio.get_event_loop().run_in_executor(copy_executor, copy_all_overrides)
    push_log(request_id, "✅ Copied all folders from override directory")


def build_zip_to_tempfile(server_dir: str, archive_root_name: str) -> str:
    """Create ZIP file with validation and error handling."""
    if not os.path.exists(server_dir):
        raise ValueError(f"Server directory does not exist: {server_dir}")
    
    # Sanitize archive_root_name
    archive_root_name = re.sub(r'[^a-zA-Z0-9_-]', '_', archive_root_name)[:MAX_FILENAME_LENGTH]
    
    temp_fd = None
    temp_zip_path = None
    try:
        temp_fd, temp_zip_path = tempfile.mkstemp(suffix=".zip")
        os.close(temp_fd)
        temp_fd = None

        file_count = 0
        max_files = 100000  # Prevent zip bomb
        max_size = 10 * 1024**3  # 10GB max ZIP size
        total_size = 0
        
        with zipfile.ZipFile(temp_zip_path, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for dirpath, _, filenames in os.walk(server_dir):
                for filename in filenames:
                    if file_count >= max_files:
                        raise ValueError(f"Too many files (max: {max_files})")
                    
                    abs_path = os.path.join(dirpath, filename)
                    
                    # Check file size
                    try:
                        file_size = os.path.getsize(abs_path)
                        total_size += file_size
                        if total_size > max_size:
                            raise ValueError(f"ZIP size exceeds maximum: {max_size} bytes")
                    except OSError:
                        continue  # Skip files we can't access
                    
                    # Sanitize path
                    rel_path = os.path.relpath(abs_path, server_dir).replace(os.path.sep, '/')
                    # Prevent path traversal in ZIP
                    if '..' in rel_path or rel_path.startswith('/'):
                        continue
                    
                    # Use rel_path directly (no subdirectory in ZIP)
                    arcname = rel_path
                    zf.write(abs_path, arcname)
                    file_count += 1
                    
                    # Log progress every 100 files
                    if file_count % 100 == 0:
                        logging.debug(f"ZIP progress: {file_count} files added...")

        logging.info(f"✅ ZIP created: {temp_zip_path} with {file_count} files ({total_size} bytes)")
        return temp_zip_path

    except Exception as e:
        # Clean up on error
        if temp_zip_path and os.path.exists(temp_zip_path):
            try:
                os.remove(temp_zip_path)
            except OSError:
                pass
        logging.exception("❌ Failed to create ZIP file")
        raise RuntimeError("ZIP generation failed") from e
    

@app.route("/download/<request_id>")
def download_zip(request_id):
    """Download generated server ZIP with validation."""
    # Validate request_id
    request_id, error = validate_request_id(request_id)
    if error:
        return jsonify({"error": error}), 400
    
    server_dir = None
    matched_name = None
    
    # Safely find server directory
    try:
        if not os.path.exists(PERSISTENT_TEMP_ROOT):
            return jsonify({"error": "Server files not found"}), 404
        
        for d in os.listdir(PERSISTENT_TEMP_ROOT):
            # Validate directory name to prevent path traversal
            if not d.endswith("-MSFG") or request_id not in d:
                continue
            # Ensure request_id matches exactly (not just substring)
            if f"-{request_id}-MSFG" in d or d == f"{request_id}-MSFG":
                server_dir = os.path.join(PERSISTENT_TEMP_ROOT, d)
                # Validate it's actually a directory and within our temp root
                if os.path.isdir(server_dir) and os.path.commonpath([PERSISTENT_TEMP_ROOT, server_dir]) == PERSISTENT_TEMP_ROOT:
                    matched_name = d.replace("-MSFG", "")
                    break
    except (OSError, ValueError) as e:
        logging.error(f"[{request_id}] Error finding server directory: {e}")
        return jsonify({"error": "Server files not found"}), 404

    if not server_dir or not os.path.exists(server_dir):
        logging.warning(f"[{request_id}] ❌ Server directory not found.")
        return jsonify({"error": "Server files not found"}), 404

    try:
        if not os.listdir(server_dir):
            logging.error(f"[{request_id}] 🚫 Server directory is empty! Aborting ZIP creation.")
            return jsonify({"error": "No files to zip. Server setup might have failed."}), 400
    except OSError:
        return jsonify({"error": "Cannot access server directory"}), 500

    archive_root_name = f"{matched_name}-MSFG"
    logging.info(f"[{request_id}] 🔍 Creating ZIP from {server_dir}")

    try:
        # Check if ZIP is already built
        with download_status_lock:
            if request_id in download_status and download_status[request_id].get("zip_path"):
                zip_path = download_status[request_id]["zip_path"]
            else:
                zip_path = build_zip_to_tempfile(server_dir, archive_root_name)
                download_status[request_id] = {"zip_path": zip_path, "ready": True, "cleanup_scheduled": False}

        if not os.path.exists(zip_path) or os.path.getsize(zip_path) < 200:
            logging.error(f"[{request_id}] 🚫 Generated ZIP is too small or missing: {zip_path}")
            return jsonify({"error": "Generated ZIP is invalid or empty."}), 500

        with zipfile.ZipFile(zip_path, 'r') as zf:
            if zf.testzip() is not None:
                raise RuntimeError("ZIP file is corrupt")

        @after_this_request
        def schedule_cleanup(response):
            with download_status_lock:
                if not download_status[request_id].get("cleanup_scheduled"):
                    threading.Thread(
                        target=delayed_cleanup,
                        args=(zip_path, server_dir, request_id, CLEANUP_DELAY),
                        daemon=True
                    ).start()
                    download_status[request_id]["cleanup_scheduled"] = True
            return response

        return send_file(
            zip_path,
            mimetype="application/zip",
            as_attachment=True,
            download_name=f"{archive_root_name}.zip",
            conditional=True
        )
    except Exception as e:
        logging.exception(f"[{request_id}] ❌ Failed to build/send ZIP")
        return jsonify({"error": "ZIP generation failed"}), 500


def push_stats():
    """Background task to push system statistics."""
    while True:
        try:
            socketio.sleep(1)
            
            # CPU stats
            cpu = psutil.cpu_percent(interval=None)
            cpu_count = psutil.cpu_count(logical=True)
            cpu_count_physical = psutil.cpu_count(logical=False)
            cpu_freq = psutil.cpu_freq()
            cpu_freq_current = round(cpu_freq.current, 2) if cpu_freq else 0
            
            # Memory stats
            ram = psutil.virtual_memory()
            swap = psutil.swap_memory()
            
            # Disk stats
            disk_root = 'C:\\' if platform.system() == 'Windows' else '/'
            disk = psutil.disk_usage(disk_root)
            
            # Network stats
            try:
                net_io = psutil.net_io_counters()
                net_bytes_sent = round(net_io.bytes_sent / (1024 ** 3), 2)
                net_bytes_recv = round(net_io.bytes_recv / (1024 ** 3), 2)
            except:
                net_bytes_sent = 0
                net_bytes_recv = 0
            
            # Process stats
            try:
                process_count = len(psutil.pids())
            except:
                process_count = 0
            
            # Boot time and uptime
            boot_time = datetime.datetime.fromtimestamp(psutil.boot_time())
            uptime = str(datetime.datetime.now() - boot_time)

            with generated_server_lock:
                count = generated_server_count

            stats = {
                # CPU
                "cpu": cpu,
                "cpu_count": cpu_count,
                "cpu_count_physical": cpu_count_physical,
                "cpu_freq": cpu_freq_current,
                
                # RAM
                "ram_percent": ram.percent,
                "ram_used": round(ram.used / (1024 ** 3), 2),
                "ram_available": round(ram.available / (1024 ** 3), 2),
                "ram_total": round(ram.total / (1024 ** 3), 2),
                "ram_free": round(ram.free / (1024 ** 3), 2),
                
                # Swap
                "swap_percent": swap.percent,
                "swap_used": round(swap.used / (1024 ** 3), 2),
                "swap_total": round(swap.total / (1024 ** 3), 2),
                
                # Disk
                "disk_percent": disk.percent,
                "disk_used": round(disk.used / (1024 ** 3), 2),
                "disk_free": round(disk.free / (1024 ** 3), 2),
                "disk_total": round(disk.total / (1024 ** 3), 2),
                
                # Network
                "net_sent": net_bytes_sent,
                "net_recv": net_bytes_recv,
                
                # System
                "uptime": uptime,
                "boot_time": boot_time.isoformat(),
                "active_users": len(active_users),
                "platform": platform.system(),
                "platform_release": platform.release(),
                "platform_version": platform.version(),
                "processor": platform.processor(),
                "process_count": process_count,
                "generated_servers": count
            }

            socketio.emit("stats", stats, namespace="/admin")
        except Exception as e:
            logging.error(f"Error in push_stats: {e}")
            socketio.sleep(5)  # Wait longer on error


@socketio.on('connect', namespace="/admin")
def handle_connect():
    print("Admin connected to live stats socket")


@socketio.on('disconnect', namespace="/admin")
def handle_disconnect():
    print("Admin disconnected from live stats socket")


# Start the background stats emitter
socketio.start_background_task(target=push_stats)

# Start log buffer cleanup task
start_log_cleanup_task()


@app.route("/admin/dashboard")
@require_admin
def admin_dashboard():
    """Admin dashboard with system statistics."""
    username = session.get("admin_user", "Admin")
    return render_template("admin_stats.html", username=username)

@app.route("/admin/users", methods=["GET"])
@require_admin
def admin_users_view():
    """View admin users management page."""
    return render_template("admin_users.html")

@app.route("/admin/api/users", methods=["GET"])
@require_admin
def get_admin_users():
    """API endpoint to get list of admin users (without passwords)."""
    users = load_admin_users()
    return jsonify({
        "users": list(users.keys()),
        "count": len(users)
    })

@app.route("/admin/api/users", methods=["POST"])
@require_admin
def add_admin_user():
    """API endpoint to add a new admin user."""
    username = session.get("admin_user")
    data = request.get_json()
    
    new_username = data.get("username", "").strip()
    new_password = data.get("password", "").strip()
    
    if not new_username or not new_password:
        return jsonify({"error": "Username and password are required"}), 400
    
    if len(new_password) < 8:
        return jsonify({"error": "Password must be at least 8 characters long"}), 400
    
    users = load_admin_users()
    if new_username in users:
        return jsonify({"error": "User already exists"}), 400
    
    # Hash password
    hashed = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
    users[new_username] = hashed.encode()
    
    if save_admin_users(users):
        log_admin_action(username, "add_user", {"new_user": new_username})
        return jsonify({"success": True, "message": f"User {new_username} added successfully"})
    else:
        return jsonify({"error": "Failed to save user"}), 500

@app.route("/admin/api/users/<user_to_delete>", methods=["DELETE"])
@require_admin
def delete_admin_user(user_to_delete):
    """API endpoint to delete an admin user."""
    username = session.get("admin_user")
    
    # Protect pokedb user from deletion
    if user_to_delete == "pokedb":
        return jsonify({"error": "Cannot delete protected user 'pokedb'"}), 400
    
    if user_to_delete == username:
        return jsonify({"error": "Cannot delete your own account"}), 400
    
    users = load_admin_users()
    if user_to_delete not in users:
        return jsonify({"error": "User not found"}), 404
    
    del users[user_to_delete]
    
    if save_admin_users(users):
        log_admin_action(username, "delete_user", {"deleted_user": user_to_delete})
        return jsonify({"success": True, "message": f"User {user_to_delete} deleted successfully"})
    else:
        return jsonify({"error": "Failed to delete user"}), 500

@app.route("/admin/api/users/change-password", methods=["POST"])
@require_admin
def change_password():
    """API endpoint to change password for current user."""
    username = session.get("admin_user")
    data = request.get_json()
    
    current_password = data.get("current_password", "").strip()
    new_password = data.get("new_password", "").strip()
    
    if not current_password or not new_password:
        return jsonify({"error": "Current and new passwords are required"}), 400
    
    if len(new_password) < 8:
        return jsonify({"error": "New password must be at least 8 characters long"}), 400
    
    users = load_admin_users()
    if username not in users:
        return jsonify({"error": "User not found"}), 404
    
    # Verify current password
    if not bcrypt.checkpw(current_password.encode(), users[username]):
        return jsonify({"error": "Current password is incorrect"}), 400
    
    # Update password
    hashed = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
    users[username] = hashed.encode()
    
    if save_admin_users(users):
        log_admin_action(username, "change_password", {})
        return jsonify({"success": True, "message": "Password changed successfully"})
    else:
        return jsonify({"error": "Failed to change password"}), 500



def push_log(request_id, message):
    """Thread-safe log pushing with validation and improved concurrency handling."""
    if not request_id or not isinstance(request_id, str):
        # Don't log warning here to avoid recursion
        return
    
    # Sanitize message to prevent log injection
    safe_message = str(message).replace('\n', ' ').replace('\r', '')[:1000]
    log_line = f"{safe_message}"
    
    # Thread-safe lock creation for new request_ids
    if request_id not in log_locks:
        with _log_buffer_creation_lock:
            # Double-check pattern to avoid race condition
            if request_id not in log_locks:
                log_locks[request_id] = Lock()
                log_buffers[request_id] = []
    
    try:
        with log_locks[request_id]:
            if request_id not in log_buffers:
                log_buffers[request_id] = []
            log_buffers[request_id].append(log_line)
            # Keep buffer size reasonable (last 10000 lines)
            if len(log_buffers[request_id]) > 10000:
                log_buffers[request_id] = log_buffers[request_id][-10000:]
    except Exception as e:
        # Use print instead of logging to avoid recursion
        print(f"Error pushing log for {request_id}: {e}", file=sys.stderr)
    
    # Only log to console if not already in a logging handler (avoid recursion)
    # The WebLogHandler will pick this up and push to web interface
    try:
        logging.info(f"[{request_id}] {safe_message}")
    except Exception:
        # Fallback to print if logging fails
        print(f"[{request_id}] {safe_message}", file=sys.stderr)
    

@app.route("/api/logs/<request_id>")
def stream_logs(request_id):
    """Stream logs for a specific request ID."""
    # Validate request_id
    request_id, error = validate_request_id(request_id)
    if error:
        return jsonify({"error": error}), 400
    
    def generate():
        try:
            # Initialize log buffer and lock if they don't exist
            if request_id not in log_locks:
                log_locks[request_id] = Lock()
            if request_id not in log_buffers:
                with log_locks[request_id]:
                    log_buffers[request_id] = []
            
            # Send initial connection message
            yield f"data: ✅ Connected to log stream\n\n"
            
            last_index = 0
            # Send any existing logs first
            with log_locks[request_id]:
                logs = log_buffers[request_id] if request_id in log_buffers else []
                for line in logs:
                    try:
                        yield f"data: {line}\n\n"
                    except GeneratorExit:
                        return
                last_index = len(logs)
            
            # Keep-alive counter
            keepalive_counter = 0
            
            # Then continue streaming new logs
            max_iterations = 36000  # 3 hours max (0.3s * 36000 = 10800s)
            iteration = 0
            while iteration < max_iterations:
                try:
                    time.sleep(0.3)  # Check more frequently
                    keepalive_counter += 1
                    iteration += 1
                    
                    # Send keep-alive every 10 seconds (every ~33 iterations)
                    if keepalive_counter % 33 == 0:
                        yield f": keepalive\n\n"
                    
                    new_logs = []
                    with log_locks[request_id]:
                        if request_id in log_buffers:
                            logs = log_buffers[request_id]
                            if last_index < len(logs):
                                new_logs = logs[last_index:]
                                last_index = len(logs)
                    
                    # Send all new logs
                    for line in new_logs:
                        try:
                            yield f"data: {line}\n\n"
                        except GeneratorExit:
                            # Client disconnected
                            logging.info(f"[{request_id}] Log stream client disconnected")
                            return
                        except Exception as e:
                            logging.error(f"[{request_id}] Error sending log line: {e}")
                            continue
                except GeneratorExit:
                    logging.info(f"[{request_id}] Log stream client disconnected (GeneratorExit)")
                    return
                except Exception as e:
                    logging.error(f"[{request_id}] Error in log stream loop: {e}")
                    # Continue trying instead of breaking
                    time.sleep(1)
                    continue
        except Exception as e:
            logging.error(f"[{request_id}] Fatal error in log stream: {e}")
            yield f"data: ❌ Log stream error: {str(e)}\n\n"
    
    return Response(generate(), mimetype="text/event-stream", headers={
        'Cache-Control': 'no-cache',
        'X-Accel-Buffering': 'no',
        'Connection': 'keep-alive'
    })

@app.route("/", methods=["GET"])
def home():
    logging.info("GET / - Rendering index.html")
    return render_template("index.html")


def validate_request_id(request_id):
    """Validate and sanitize request ID to prevent path traversal."""
    if not request_id:
        return None, "Missing request ID"
    
    # Only allow alphanumeric, hyphens, and underscores
    if not re.match(r'^[a-zA-Z0-9_-]+$', request_id):
        return None, "Invalid request ID format"
    
    # Limit length to prevent abuse
    if len(request_id) > 100:
        return None, "Request ID too long"
    
    return request_id, None


@app.route("/api/generate", methods=["POST"])
def generate_server():
    request_id = request.args.get("request_id")
    request_id, error = validate_request_id(request_id)
    if error:
        return jsonify({"error": error}), 400
    
    # Set request_id in thread-local storage for logging handler
    _thread_local.request_id = request_id
    
    try:
        # Validate file upload
        if 'mrpack' not in request.files:
            return jsonify({"error": "No file uploaded"}), 400
        
        mrpack_file = request.files['mrpack']
        if not mrpack_file.filename:
            return jsonify({"error": "No filename provided"}), 400
        
        # Validate file extension
        if not mrpack_file.filename.lower().endswith('.mrpack'):
            return jsonify({"error": "Invalid file type. Only .mrpack files are allowed."}), 400
        
        # Initialize log buffer and lock for this request EARLY, before any operations
        # This ensures logs are captured even if the stream connects late
        if request_id not in log_locks:
            log_locks[request_id] = Lock()
        with log_locks[request_id]:
            if request_id not in log_buffers:
                log_buffers[request_id] = []
        
        push_log(request_id, "🛠 Starting server generation...")
        
        with tempfile.TemporaryDirectory() as tmp_dir:
            try:
                # Extract .mrpack
                mrpack_path = os.path.join(tmp_dir, "modpack.mrpack")
                mrpack_file.save(mrpack_path)
                extract_path = os.path.join(tmp_dir, "extracted")
                os.makedirs(extract_path, exist_ok=True)
                with zipfile.ZipFile(mrpack_path, 'r') as zip_ref:
                    zip_ref.extractall(extract_path)
                push_log(request_id, "Extracted .mrpack successfully")
                # Read index
                index_file = os.path.join(extract_path, "modrinth.index.json")
                with open(index_file) as f:
                    index_data = json.load(f)
                deps = index_data["dependencies"]
                mc_version = deps.get("minecraft")
                loader_type, loader_version = detect_loader(deps)
                push_log(request_id, f"Detected Minecraft {mc_version} with loader {loader_type} {loader_version}")
                # Setup server directory using sanitized .mrpack name
                base_name = os.path.splitext(mrpack_file.filename)[0]
                logging.info(f"[{request_id}] Modpack name: {base_name}")
                
                # Sanitize filename: remove path components and dangerous characters
                base_name = os.path.basename(base_name)  # Remove any path components
                base_name = base_name.replace(" ", "_")
                base_name = re.sub(r'[^a-zA-Z0-9_-]', '_', base_name)  # Only allow safe chars
                base_name = base_name[:MAX_FILENAME_LENGTH]  # Limit length
                safe_name = base_name or "server"  # Fallback if name becomes empty
                logging.info(f"[{request_id}] Safe name for server directory: {safe_name}")
                server_dir_name = f"{safe_name}-{request_id}-MSFG"
                server_dir = os.path.join(PERSISTENT_TEMP_ROOT, server_dir_name)
                logging.info(f"[{request_id}] Server directory set to: {server_dir}")
                os.makedirs(os.path.join(server_dir, "mods"), exist_ok=True)
                
                try:
                    push_log(request_id, "🔁 Starting async mod download phase")
                    logging.info(f"[{request_id}] 🔁 Starting async mod download phase")
                    push_log(request_id, f"📦 Mod list contains {len(index_data['files'])} entries")
                    asyncio.run(parallel_download_and_copy_async(index_data, extract_path, server_dir, request_id))
                    push_log(request_id, "✅ Downloaded mods and copied overrides successfully")
                    logging.info(f"[{request_id}] Downloaded mods and copied overrides successfully")
                except Exception as e:
                    push_log(request_id, f"❌ Async mod download failed: {e}")
                    logging.exception(f"[{request_id}] Exception during mod download and override copy")
                    return jsonify({"error": "Failed to download mods or copy overrides"}), 500
                if loader_type == 'quilt':
                    try:
                        push_log(request_id, f"🧵 Starting Quilt server setup for Minecraft {mc_version} with Quilt {loader_version}")
                        setup_quilt(mc_version, loader_version, server_dir, request_id)
                        push_log(request_id, "✅ Quilt server setup completed successfully")
                    except Exception as e:
                        push_log(request_id, f"❌ Server setup error: {e}")
                        logging.error(f"[{request_id}] Server setup error: {e}")
                        try:
                            error_data = json.loads(str(e))
                            return jsonify(error_data), 500
                        except json.JSONDecodeError:
                            return jsonify({"error": str(e)}), 500
                else:
                    try:
                        if loader_type == 'fabric':
                            push_log(request_id, f"🧩 Starting Fabric server setup for Minecraft {mc_version} with Fabric {loader_version}")
                            setup_fabric(mc_version, loader_version, server_dir, request_id)
                            push_log(request_id, "✅ Fabric server setup completed successfully")
                        elif loader_type == 'forge':
                            push_log(request_id, f"⚙️ Starting Forge server setup for Minecraft {mc_version} with Forge {loader_version}")
                            setup_forge(mc_version, loader_version, server_dir, request_id)
                            push_log(request_id, "✅ Forge server setup completed successfully")
                        elif loader_type == 'neoforge':
                            push_log(request_id, f"🔧 Starting NeoForge server setup for Minecraft {mc_version} with NeoForge {loader_version}")
                            setup_neoforge(mc_version, loader_version, server_dir, request_id, include_starter_jar=True)
                            push_log(request_id, "✅ NeoForge server setup completed successfully")
                    except Exception as e:
                        push_log(request_id, f"❌ Server setup error: {e}")
                        logging.error(f"[{request_id}] Server setup error: {e}")
                        try:
                            error_data = json.loads(str(e))
                            return jsonify(error_data), 500
                        except json.JSONDecodeError:
                            return jsonify({"error": str(e)}), 500
                with generated_server_lock:
                    count = increment_generated_server_count()
                    push_log(request_id, f"✅ Server setup complete. Total servers generated: {count}")
                
                # Create ZIP file
                push_log(request_id, "📦 Creating server ZIP archive...")
                archive_root_name = f"{safe_name}-MSFG"
                try:
                    zip_path = build_zip_to_tempfile(server_dir, archive_root_name)
                    zip_size_mb = os.path.getsize(zip_path) / (1024 * 1024)
                    push_log(request_id, f"✅ ZIP created successfully ({zip_size_mb:.2f} MB)")
                except Exception as zip_error:
                    push_log(request_id, f"❌ Failed to create ZIP: {zip_error}")
                    logging.exception(f"[{request_id}] ZIP creation failed: {zip_error}")
                    return jsonify({"error": "Failed to create ZIP archive", "message": str(zip_error)}), 500
                
                with download_status_lock:
                    download_status[request_id] = {"zip_path": zip_path, "ready": True, "cleanup_scheduled": False}
                
                push_log(request_id, "✅ ZIP ready!")
                push_log(request_id, "🎉 Server generation complete! Download will start automatically...")
                
                return jsonify({
                    "status": "success",
                    "message": "Server files generated successfully",
                    "download_url": f"/download/{request_id}"
                }), 200
            except Exception as e:
                push_log(request_id, f"❌ Unexpected error: {e}")
                logging.exception(f"[{request_id}] Unexpected error: {e}")
                try:
                    error_data = json.loads(str(e))
                    return jsonify(error_data), 500
                except json.JSONDecodeError:
                    return jsonify({"error": "Internal server error"}), 500
    finally:
        # Clear request_id from thread-local storage
        if hasattr(_thread_local, 'request_id'):
            delattr(_thread_local, 'request_id')
            

def detect_loader(deps):
    for loader in ['fabric-loader', 'forge', 'quilt-loader', 'neoforge']:
        if loader in deps:
            return loader.split('-')[0], deps[loader]


@app.route("/api/check_loader", methods=["POST"])
def check_loader():
    if 'mrpack' not in request.files:
        return jsonify({"error": "No .mrpack file uploaded"}), 400

    mrpack_file = request.files['mrpack']
    with tempfile.TemporaryDirectory() as tmp_dir:
        path = os.path.join(tmp_dir, "modpack.mrpack")
        mrpack_file.save(path)

        with zipfile.ZipFile(path, 'r') as zip_ref:
            zip_ref.extractall(tmp_dir)

        index_path = os.path.join(tmp_dir, "modrinth.index.json")
        with open(index_path) as f:
            data = json.load(f)

        loader_type, _ = detect_loader(data["dependencies"])
        return jsonify({"loader": loader_type})

@app.route("/quilt", methods=["GET"])
def quilt():
    logging.info("GET /quilt - Rendering quilt.html")
    return render_template("quilt.html")


def download_to_file(url, dest, request_id, max_size=MAX_UPLOAD_SIZE, retries=3):
    """Download file with size validation, error handling, and retry logic."""
    # Set request_id in thread-local storage so WebLogHandler can push logs to web interface
    if request_id:
        _thread_local.request_id = request_id
    session = get_http_session()
    
    for attempt in range(retries):
        try:
            if attempt > 0:
                # Exponential backoff: 1s, 2s, 4s
                wait_time = 2 ** (attempt - 1)
                push_log(request_id, f"Retrying download (attempt {attempt + 1}/{retries}) after {wait_time}s...")
                time.sleep(wait_time)
            
            push_log(request_id, f"Downloading: {url}" + (f" (attempt {attempt + 1}/{retries})" if attempt > 0 else ""))
            
            # Validate URL
            if not url or not isinstance(url, str):
                raise ValueError("Invalid URL")
            
            with session.get(url, stream=True, timeout=30, allow_redirects=True) as r:
                r.raise_for_status()
                
                # Check content length
                content_length = r.headers.get('Content-Length')
                if content_length and int(content_length) > max_size:
                    raise ValueError(f"File too large: {content_length} bytes")
                
                downloaded = 0
                with open(dest, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=CHUNK_SIZE):
                        if chunk:
                            downloaded += len(chunk)
                            if downloaded > max_size:
                                os.remove(dest)
                                raise ValueError(f"File exceeds maximum size: {max_size} bytes")
                            f.write(chunk)
            
            push_log(request_id, f"Downloaded to {dest}")
            return  # Success, exit retry loop
            
        except requests.exceptions.RequestException as e:
            if attempt == retries - 1:
                # Last attempt failed
                if os.path.exists(dest):
                    try:
                        os.remove(dest)
                    except OSError:
                        pass
                logging.error(f"[{request_id}] Failed to download {url} after {retries} attempts: {e}")
                raise
            # Continue to retry
            continue
        except Exception as e:
            # Non-retryable error or last attempt
            if os.path.exists(dest):
                try:
                    os.remove(dest)
                except OSError:
                    pass
            logging.error(f"[{request_id}] Failed to download {url}: {e}")
            raise


def copy_overrides(src, dst, request_id):
    # Set request_id in thread-local storage so WebLogHandler can push logs to web interface
    if request_id:
        _thread_local.request_id = request_id
    for root, _, files in os.walk(src):
        rel_path = os.path.relpath(root, src)
        dest_dir = os.path.join(dst, rel_path)
        os.makedirs(dest_dir, exist_ok=True)
        for file in files:
            shutil.copy2(os.path.join(root, file), os.path.join(dest_dir, file))
            push_log(request_id, f"Copied override: {file}")


def run_installer(java_path, installer_path, args, server_dir, request_id, queue):
    try:
        os.environ['GEVENT_MONKEY_PATCH'] = '0'
        
        if not os.path.exists(java_path):
            queue.put(("LOG", f"❌ Java not found at {java_path}"))
            queue.put((-1, f"Java not found at {java_path}"))
            return
        
        # Set a longer timeout for NeoForge specifically
        is_neoforge = "neoforge" in installer_path.lower()
        timeout_seconds = NEOFORGE_TIMEOUT if is_neoforge else DEFAULT_INSTALLER_TIMEOUT
        
        process = subprocess.Popen(
            [java_path, "-jar", installer_path] + args,
            cwd=server_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            universal_newlines=True
        )
        
        output = []
        start_time = time.time()
        last_output_time = start_time
        
        while True:
            line = process.stdout.readline().strip()
            if line:
                last_output_time = time.time()  # Update last output time
                queue.put(("LOG", f"Installer: {line}"))
                output.append(line)
                
                # Check for success messages even if process doesn't exit
                if "The server installed successfully" in line or "Installer completed with code 0" in line:
                    queue.put(("LOG", "✅ Installation completed successfully"))
                    # Give it a moment to finish up
                    time.sleep(2)  # Reduced from 5 to 2 seconds
                    break
            
            if process.poll() is not None:
                break
                
            # Use a more sophisticated timeout check
            # If no output for configured timeout, consider it stuck
            if time.time() - last_output_time > NO_OUTPUT_TIMEOUT:
                queue.put(("LOG", "⚠️ No output for 5 minutes, assuming process is stuck"))
                process.terminate()
                output.append("Installer timed out due to no output")
                queue.put((-1, "\n".join(output)))
                return
                
            # Overall timeout
            if time.time() - start_time > timeout_seconds:
                queue.put(("LOG", f"⚠️ Overall timeout of {timeout_seconds} seconds reached"))
                process.terminate()
                output.append(f"Installer timed out after {timeout_seconds} seconds")
                queue.put((-1, "\n".join(output)))
                return
        
        returncode = process.wait()
        # Read any remaining output
        remaining_output = process.stdout.read()
        if remaining_output:
            # Push remaining output line by line
            for remaining_line in remaining_output.splitlines():
                if remaining_line.strip():
                    queue.put(("LOG", f"Installer: {remaining_line.strip()}"))
            output.append(remaining_output)
        queue.put(("LOG", f"Installer completed with code {returncode}"))
        queue.put((returncode, "\n".join(output)))
    except Exception as e:
        queue.put(("LOG", f"❌ Subprocess error: {str(e)}"))
        queue.put((-1, f"Subprocess error: {str(e)}"))


def setup_fabric(mc_version, loader_version, server_dir, request_id):
    """
    Setup Fabric server in the given server_dir.
    Note: server_dir already contains mods/, config/, and other override files from the modpack.
    This function only installs the Fabric server JAR and creates server.jar.
    """
    # Set request_id in thread-local storage so WebLogHandler can push logs to web interface
    _thread_local.request_id = request_id
    push_log(request_id, f"📥 Checking for Fabric installer...")
    installer_path = os.path.join(tempfile.gettempdir(), f"fabric-installer-{request_id}.jar")
    if not os.path.exists(installer_path):
        meta_url = "https://meta.fabricmc.net/v2/versions/installer"
        push_log(request_id, f"📡 Fetching Fabric installer metadata from {meta_url}")
        try:
            installer_meta = requests.get(meta_url, timeout=15).json()[0]
            push_log(request_id, f"⬇️ Downloading Fabric installer from {installer_meta['url']}")
            download_to_file(installer_meta['url'], installer_path, request_id)
            push_log(request_id, f"✅ Fabric installer downloaded successfully")
        except Exception as e:
            error_msg = f"Failed to fetch Fabric installer: {str(e)}. Download the Fabric installer manually from: {meta_url}"
            push_log(request_id, f"❌ {error_msg}")
            raise RuntimeError(json.dumps({
                "error": "Installer fetch failed",
                "message": str(e),
                "download_link": meta_url
            }))
    if not os.access(server_dir, os.W_OK):
        push_log(request_id, f"No write permissions for {server_dir}")
        raise RuntimeError(json.dumps({
            "error": "Permission denied",
            "message": f"Cannot write to server directory: {server_dir}",
            "download_link": meta_url
        }))
    push_log(request_id, f"☕ Resolving Java version for Fabric {mc_version}...")
    java_version = resolve_java_version("fabric", mc_version)
    java_path = get_java_path(java_version)
    push_log(request_id, f"☕ Using Java {java_version} at {java_path}")
    push_log(request_id, f"🚀 Starting Fabric installer process...")
    queue = Queue()
    args = ["server", "-downloadMinecraft", "-mcversion", mc_version, "-loader", loader_version, "-dir", server_dir]
    push_log(request_id, f"📋 Installer arguments: {' '.join(args)}")
    p = Process(target=run_installer, args=(java_path, installer_path, args, server_dir, request_id, queue))
    p.start()
    push_log(request_id, f"⏳ Waiting for Fabric installer to complete...")
    
    # Read logs from queue in real-time while process is running
    returncode = None
    stdout = None
    start_time = time.time()
    max_wait_time = 360  # 6-minute timeout
    last_log_time = start_time
    
    while p.is_alive() and (time.time() - start_time) < max_wait_time:
        try:
            # Try to get log messages or result from queue with timeout
            item = queue.get(timeout=1)
            if isinstance(item, tuple) and len(item) == 2:
                if item[0] == "LOG":
                    # This is a log message, push it
                    push_log(request_id, item[1])
                    last_log_time = time.time()
                else:
                    # This is the result tuple (returncode, stdout)
                    returncode, stdout = item
                    break
        except Exception:
            # Queue is empty or timeout, continue waiting
            # Check if process is still alive
            if not p.is_alive():
                break
            # Check if we've had no logs for too long (but less than total timeout)
            if time.time() - last_log_time > 60:  # 1 minute without logs
                push_log(request_id, "⏳ Waiting for installer output...")
                last_log_time = time.time()
            # Small sleep to avoid busy waiting
            time.sleep(0.1)
    
    # Wait for process to finish if still running
    if p.is_alive():
        p.join(timeout=5)
        if p.is_alive():
            p.terminate()
            error_msg = f"Fabric installer process timed out. Download the Fabric installer manually from: {meta_url}"
            push_log(request_id, f"❌ {error_msg}")
            raise RuntimeError(json.dumps({
                "error": "Installer timeout",
                "message": "Fabric installer process took too long.",
                "download_link": meta_url
            }))
    
    # Get result from queue if we haven't already
    if returncode is None:
        try:
            # Try to get any remaining items from queue
            while True:
                try:
                    item = queue.get(timeout=1)
                    if isinstance(item, tuple) and len(item) == 2:
                        if item[0] == "LOG":
                            push_log(request_id, item[1])
                        else:
                            returncode, stdout = item
                            break
                except Exception:
                    break
        except Exception:
            pass
    
    # If we still don't have a result, try one more time
    if returncode is None:
        try:
            returncode, stdout = queue.get(timeout=5)
            # Make sure it's not a LOG message
            if isinstance(returncode, str) and returncode == "LOG":
                # This was a LOG message, try again
                returncode, stdout = queue.get(timeout=5)
        except Exception:
            returncode, stdout = -1, "Failed to get installer output from queue"
    
    push_log(request_id, f"✅ Fabric installer process completed with exit code {returncode}")
    if stdout:
        push_log(request_id, f"📋 Fabric installer final output: {stdout[:500]}...")  # Limit output length
    if returncode != 0:
        error_msg = f"Fabric installer failed with exit code {returncode}. Download the Fabric installer manually from: {meta_url}"
        push_log(request_id, f"❌ {error_msg}")
        raise RuntimeError(json.dumps({
            "error": "Fabric installer failed",
            "message": stdout,
            "download_link": meta_url
        }))
    push_log(request_id, f"📁 Checking server directory for JAR files...")
    dir_contents = os.listdir(server_dir)
    push_log(request_id, f"📂 Server directory contains: {len(dir_contents)} items")
    jar_files = [f for f in dir_contents if f.endswith('.jar') and 'installer' not in f.lower()]
    push_log(request_id, f"🔍 Found {len(jar_files)} potential server JAR file(s)")
    if not jar_files:
        push_log(request_id, "Failed to find Fabric server JAR files.")
        jar_files = [f for f in os.listdir(server_dir) if f.endswith('.jar') and 'installer' not in f.lower()]
        if not jar_files:
            error_msg = f"No JAR files found. Download the Fabric installer manually from: {meta_url}"
            push_log(request_id, f"❌ {error_msg}")
            raise RuntimeError(json.dumps({
                "error": "No JAR files found",
                "message": "Fabric setup failed to generate a server JAR.",
                "download_link": meta_url
            }))
    if len(jar_files) < 1:
        error_msg = f"Expected at least one JAR file, but found {len(jar_files)}. Download the Fabric installer manually from: {meta_url}"
        push_log(request_id, f"❌ {error_msg}")
        raise RuntimeError(json.dumps({
            "error": "No valid JAR files found",
            "message": "Fabric setup failed to generate a valid server JAR.",
            "download_link": meta_url
        }))
    push_log(request_id, f"Found {len(jar_files)} JAR files: {', '.join(jar_files)}")
    
    # Prioritize server JAR files - look for actual server JAR, not launcher or installer
    server_jar_candidates = []
    for jar_file in jar_files:
        jar_lower = jar_file.lower()
        # Skip launcher and installer JARs
        if 'launch' in jar_lower or 'installer' in jar_lower or 'client' in jar_lower:
            continue
        # Prioritize files with "server" in name, or minecraft-server
        if 'server' in jar_lower or jar_lower.startswith('minecraft'):
            server_jar_candidates.insert(0, jar_file)
        else:
            server_jar_candidates.append(jar_file)
    
    # Use prioritized list or fall back to original list
    if server_jar_candidates:
        target_jar = server_jar_candidates[0]
    elif jar_files:
        target_jar = jar_files[0]
    else:
        target_jar = None
    
    if not target_jar:
        error_msg = f"server.jar not found. Download the Fabric installer manually from: {meta_url}"
        push_log(request_id, f"❌ {error_msg}")
        raise RuntimeError(json.dumps({
            "error": "server.jar not found",
            "message": "Fabric setup failed to generate server.jar.",
            "download_link": meta_url
        }))
    
    if len(jar_files) > 1:
        push_log(request_id, f"Multiple JAR files found: {', '.join(jar_files)}. Using {target_jar}.")
    
    server_jar = os.path.join(server_dir, target_jar)
    target_server_jar = os.path.join(server_dir, "server.jar")
    
    if os.path.exists(server_jar) and target_jar != "server.jar":
        push_log(request_id, f"📝 Renaming {target_jar} to server.jar...")
        if os.path.exists(target_server_jar):
            os.remove(target_server_jar)  # Remove old server.jar if it exists
        os.rename(server_jar, target_server_jar)
        push_log(request_id, f"✅ Renamed {target_jar} to server.jar")
    elif os.path.exists(target_server_jar):
        push_log(request_id, f"✅ server.jar already exists and is ready")
    elif not os.path.exists(target_server_jar):
        error_msg = f"server.jar not found. Download the Fabric installer manually from: {meta_url}"
        push_log(request_id, f"❌ {error_msg}")
        raise RuntimeError(json.dumps({
            "error": "server.jar not found",
            "message": "Fabric setup failed to generate server.jar.",
            "download_link": meta_url
        }))
    push_log(request_id, "✅ Fabric server setup complete")
    push_log(request_id, "📝 Creating eula.txt file...")
    eula_path = os.path.join(server_dir, "eula.txt")
    with open(eula_path, "w") as f:
        f.write("eula=false\n")
    push_log(request_id, "✅ Created eula.txt with eula=false")
    push_log(request_id, f"🧹 Cleaning up Fabric installer...")
    os.remove(installer_path)
    push_log(request_id, f"✅ Deleted Fabric installer: {installer_path}")


def extract_jar_url_from_installation(installation, request_id):
    """
    Extract JAR or ZIP download URL from installation array.
    Installation is an array of arrays of InstallationStep objects.
    Each InstallationStep has: url, file, size, type
    Returns ZIP URLs with highest priority (contain server.jar and lib folder).
    """
    if not installation:
        push_log(request_id, "⚠️ Installation array is empty or None")
        return None
    
    if not isinstance(installation, list):
        push_log(request_id, f"⚠️ Installation is not a list, it's: {type(installation)}")
        return None
    
    push_log(request_id, f"🔍 Processing installation array with {len(installation)} step groups")
    
    # Collect all JAR URLs first, then prioritize
    jar_urls = []
    
    # Installation is an array of arrays
    for idx, step_group in enumerate(installation):
        if not isinstance(step_group, list):
            step_group = [step_group]
        
        push_log(request_id, f"🔍 Processing step group {idx} with {len(step_group)} steps")
        
        for step_idx, step in enumerate(step_group):
            if not isinstance(step, dict):
                push_log(request_id, f"⚠️ Step {step_idx} is not a dict: {type(step)}")
                continue
            
            step_type = step.get('type', '')
            file_name = step.get('file', '')
            url = step.get('url', '')
            
            push_log(request_id, f"🔍 Step {step_idx}: type={step_type}, file={file_name}, url={'present' if url else 'missing'}")
            
            # Check if this is a download step
            if step_type != 'download':
                continue
            
            # Look for ZIP files (preferred) or JAR files
            if (file_name.endswith('.zip') or file_name.endswith('.jar')) and url:
                file_lower = file_name.lower()
                priority = 0
                is_zip_file = file_name.endswith('.zip')
                
                # Highest priority: ZIP files (contain server.jar and lib folder)
                if is_zip_file:
                    priority = 1  # Highest priority
                    push_log(request_id, f"📥 Found high-priority ZIP: {file_name}")
                    jar_urls.append((priority, file_name, url))
                # High priority: server JAR files
                elif ('server' in file_lower or 'forge' in file_lower) and 'installer' not in file_lower and 'client' not in file_lower:
                    priority = 2
                    push_log(request_id, f"📥 Found high-priority server JAR: {file_name}")
                    jar_urls.append((priority, file_name, url))
                # Medium priority: any JAR that's not installer or client
                elif 'installer' not in file_lower and 'client' not in file_lower:
                    priority = 3
                    push_log(request_id, f"📥 Found medium-priority JAR: {file_name}")
                    jar_urls.append((priority, file_name, url))
                # Low priority: any JAR (including installer/client as last resort)
                else:
                    priority = 4
                    push_log(request_id, f"📥 Found low-priority JAR: {file_name}")
                    jar_urls.append((priority, file_name, url))
    
    # Sort by priority and return the best match
    if jar_urls:
        jar_urls.sort(key=lambda x: x[0])  # Sort by priority (lower is better)
        best_priority, best_file, best_url = jar_urls[0]
        file_type = "ZIP" if best_file.endswith('.zip') else "JAR"
        push_log(request_id, f"✅ Selected {file_type}: {best_file} (priority: {best_priority})")
        return best_url
    
    push_log(request_id, "⚠️ No JAR/ZIP files found in installation array")
    return None


def _get_forge_from_cache(version, request_id):
    """Check if Forge server file is cached and return cache info if valid."""
    with _forge_cache_lock:
        if version in _forge_cache:
            cache_info = _forge_cache[version]
            cache_path = cache_info["path"]
            
            # Check if cache file still exists and is valid
            if os.path.exists(cache_path):
                # Check cache age
                age = time.time() - cache_info["timestamp"]
                if age < FORGE_CACHE_MAX_AGE:
                    # Verify file size matches
                    if os.path.getsize(cache_path) == cache_info["size"]:
                        push_log(request_id, f"✅ Using cached Forge server file (age: {int(age/3600)}h)")
                        return cache_info
                else:
                    # Cache expired, remove it
                    try:
                        os.remove(cache_path)
                    except OSError:
                        pass
                    del _forge_cache[version]
    return None


def _save_forge_to_cache(version, file_path, is_zip, request_id):
    """Save Forge server file to cache."""
    try:
        file_size = os.path.getsize(file_path)
        
        # Check cache size limit
        total_cache_size = sum(info.get("size", 0) for info in _forge_cache.values())
        if total_cache_size + file_size > FORGE_CACHE_MAX_SIZE:
            push_log(request_id, "⚠️ Cache size limit reached, skipping cache")
            return
        
        # Create cache entry
        cache_info = {
            "path": file_path,
            "is_zip": is_zip,
            "size": file_size,
            "timestamp": time.time()
        }
        
        with _forge_cache_lock:
            _forge_cache[version] = cache_info
        
        push_log(request_id, f"💾 Cached Forge server file for future use")
    except Exception as e:
        logging.warning(f"[{request_id}] Failed to cache Forge file: {e}")


def _get_forge_download_url(mc_ver, forge_ver, request_id, retries=3):
    """Get Forge download URL from API with retry logic."""
    session = get_http_session()
    api_base_url = "https://mcjars.app/api"
    
    for attempt in range(retries):
        try:
            if attempt > 0:
                wait_time = 2 ** (attempt - 1)
                push_log(request_id, f"Retrying API request (attempt {attempt + 1}/{retries}) after {wait_time}s...")
                time.sleep(wait_time)
            
            # Query /api/v1/builds/FORGE/{mc_version} to get all builds for that MC version
            builds_url = f"{api_base_url}/v1/builds/FORGE/{mc_ver}"
            push_log(request_id, f"📡 Fetching: {builds_url}" + (f" (attempt {attempt + 1}/{retries})" if attempt > 0 else ""))
            resp = session.get(builds_url, timeout=15)
            
            if resp.status_code == 404:
                # Try with "latest" build endpoint
                latest_url = f"{api_base_url}/v1/builds/FORGE/{mc_ver}/latest"
                push_log(request_id, f"📡 Trying latest build: {latest_url}")
                resp = session.get(latest_url, timeout=15)
            
            resp.raise_for_status()
            api_response = resp.json()
            
            # Handle API response structure: {"success": true, "builds": [...]}
            if not api_response.get('success'):
                raise ValueError("API returned unsuccessful response")
            
            # Get builds list - could be direct array or nested in response
            if 'build' in api_response:
                # Single build response from /latest endpoint
                builds_list = [api_response['build']]
            elif 'builds' in api_response:
                builds_list = api_response['builds']
            elif isinstance(api_response, list):
                builds_list = api_response
            else:
                builds_list = [api_response]
            
            if not builds_list or len(builds_list) == 0:
                raise ValueError(f"No builds found for Forge {mc_ver}-{forge_ver}")
            
            # Find the build matching the Forge version
            build = None
            for b in builds_list:
                project_version = b.get('projectVersionId') or b.get('name') or ''
                # Check if Forge version matches (could be exact or partial match)
                if forge_ver in str(project_version) or str(project_version) in forge_ver:
                    build = b
                    push_log(request_id, f"✅ Found matching build: {project_version}")
                    break
            
            # If no exact match, try to find by name or use the first build
            if not build:
                push_log(request_id, f"⚠️ No exact match found, using first available build")
                build = builds_list[0]
            
            # Prefer zipUrl over jarUrl (ZIP contains server.jar and lib folder)
            download_url = build.get('zipUrl')
            is_zip = True
            
            if not download_url:
                # Try jarUrl as fallback
                download_url = build.get('jarUrl')
                is_zip = False
            
            if not download_url:
                # Try alternative field names
                download_url = (build.get('zip_url') or build.get('jar_url') or 
                              build.get('download') or build.get('url') or build.get('downloadUrl'))
                is_zip = 'zip' in str(download_url).lower() if download_url else False
            
            # If zipUrl/jarUrl is null, check installation array from original build first
            if not download_url and 'installation' in build:
                installation = build['installation']
                push_log(request_id, f"🔍 Checking installation array from original build (length: {len(installation) if isinstance(installation, list) else 'N/A'})")
                download_url = extract_jar_url_from_installation(installation, request_id)
                # Check if URL is a ZIP file
                is_zip = download_url and (download_url.lower().endswith('.zip') or '.zip' in download_url.lower()) if download_url else False
            
            # If still no download URL, try to get build details using build ID
            if not download_url:
                build_id = build.get('id')
                if build_id:
                    push_log(request_id, f"📦 Fetching build details for build ID: {build_id}")
                    build_url = f"{api_base_url}/v1/build/{build_id}"
                    build_resp = session.get(build_url, timeout=15)
                    build_resp.raise_for_status()
                    build_info = build_resp.json()
                    if build_info.get('success') and 'build' in build_info:
                        build_data = build_info['build']
                        # Prefer zipUrl
                        download_url = build_data.get('zipUrl')
                        is_zip = True
                        
                        if not download_url:
                            download_url = build_data.get('jarUrl') or build_data.get('jar_url')
                            is_zip = False
                        
                        # If still no zipUrl/jarUrl, check installation array from build details
                        if not download_url and 'installation' in build_data:
                            installation = build_data['installation']
                            push_log(request_id, f"🔍 Checking installation array from build details (length: {len(installation) if isinstance(installation, list) else 'N/A'})")
                            download_url = extract_jar_url_from_installation(installation, request_id)
                            # Check if URL is a ZIP file
                            is_zip = download_url and (download_url.lower().endswith('.zip') or '.zip' in download_url.lower()) if download_url else False
            
            if not download_url:
                raise ValueError("No zipUrl, jarUrl or installation download URL found in API response")
            
            if is_zip:
                push_log(request_id, f"✅ Found Forge server ZIP download URL")
            else:
                push_log(request_id, f"✅ Found Forge server JAR download URL")
            
            return download_url, is_zip
            
        except requests.exceptions.RequestException as e:
            if attempt == retries - 1:
                error_msg = f"Failed to query mcjars.app API after {retries} attempts: {str(e)}. Download manually from: https://files.minecraftforge.net/"
                push_log(request_id, f"❌ {error_msg}")
                raise RuntimeError(json.dumps({
                    "error": "API query failed",
                    "message": str(e),
                    "download_link": "https://files.minecraftforge.net/"
                }))
            # Continue to retry
            continue
        except (KeyError, ValueError, TypeError) as e:
            # Don't retry on these errors
            error_msg = f"Invalid response from mcjars.app API: {str(e)}. Download manually from: https://files.minecraftforge.net/"
            push_log(request_id, f"❌ {error_msg}")
            raise RuntimeError(json.dumps({
                "error": "Invalid API response",
                "message": str(e),
                "download_link": "https://files.minecraftforge.net/"
            }))


def setup_forge(mc_version, loader_version, server_dir, request_id):
    """
    Setup Forge server using mcjars.app API to download server JAR directly.
    Optimized for high concurrency with caching and connection pooling.
    """
    # Set request_id in thread-local storage so WebLogHandler can push logs to web interface
    _thread_local.request_id = request_id
    version = f"{mc_version}-{loader_version}"
    push_log(request_id, f"🔍 Setting up Forge {version}...")
    
    if not os.access(server_dir, os.W_OK):
        push_log(request_id, f"No write permissions for {server_dir}")
        raise RuntimeError(json.dumps({
            "error": "Permission denied",
            "message": f"Cannot write to server directory: {server_dir}",
            "download_link": "https://files.minecraftforge.net/"
        }))
    
    # Check cache first
    push_log(request_id, f"💾 Checking Forge cache for version {version}...")
    cache_info = _get_forge_from_cache(version, request_id)
    target_server_jar = os.path.join(server_dir, "server.jar")
    
    if cache_info:
        push_log(request_id, f"✅ Found cached Forge server files")
        # Use cached file
        cached_path = cache_info["path"]
        is_zip = cache_info["is_zip"]
        
        try:
            if is_zip:
                # Copy cached ZIP to temp location, then extract
                temp_zip = os.path.join(tempfile.gettempdir(), f"forge-server-{request_id}-{uuid.uuid4().hex[:8]}.zip")
                shutil.copy2(cached_path, temp_zip)
                push_log(request_id, f"📦 Extracting cached Forge server ZIP...")
                
                with zipfile.ZipFile(temp_zip, 'r') as zf:
                    zf.extractall(server_dir)
                
                # Check if server.jar was extracted
                if not os.path.exists(target_server_jar):
                    for root, dirs, files in os.walk(server_dir):
                        if 'server.jar' in files:
                            extracted_jar = os.path.join(root, 'server.jar')
                            if extracted_jar != target_server_jar:
                                shutil.move(extracted_jar, target_server_jar)
                                push_log(request_id, f"📁 Moved server.jar to root")
                            break
                
                # Clean up temp ZIP
                try:
                    os.remove(temp_zip)
                except OSError:
                    pass
            else:
                # Copy cached JAR directly
                shutil.copy2(cached_path, target_server_jar)
                push_log(request_id, f"✅ Copied cached Forge server JAR")
            
            # Create eula.txt
            eula_path = os.path.join(server_dir, "eula.txt")
            with open(eula_path, "w") as f:
                f.write("eula=false\n")
            push_log(request_id, "📝 Created eula.txt with eula=false")
            push_log(request_id, "Forge server setup complete (from cache)")
            return
            
        except Exception as e:
            push_log(request_id, f"⚠️ Failed to use cache, downloading fresh: {e}")
            # Fall through to download
    
    # Not in cache or cache failed, download fresh
    try:
        # Split version into MC version and Forge version
        if '-' not in version:
            raise ValueError(f"Invalid Forge version format: {version} (expected format: MC_VERSION-FORGE_VERSION)")
        
        mc_ver, forge_ver = version.split('-', 1)
        
        # Query API for download URL
        push_log(request_id, f"🔍 Querying mcjars.app API for Forge {version}")
        download_url, is_zip = _get_forge_download_url(mc_ver, forge_ver, request_id)
        
    except (KeyError, ValueError, TypeError) as e:
        error_msg = f"Invalid response from mcjars.app API: {str(e)}. Download manually from: https://files.minecraftforge.net/"
        push_log(request_id, f"❌ {error_msg}")
        raise RuntimeError(json.dumps({
            "error": "Invalid API response",
            "message": str(e),
            "download_link": "https://files.minecraftforge.net/"
        }))
    
    # Download the ZIP or JAR file
    if is_zip:
        # Download ZIP file to cache location first, then copy/extract
        safe_version = version.replace('/', '_').replace('\\', '_')
        cache_zip_path = os.path.join(_forge_cache_dir, f"forge-{safe_version}.zip")
        download_to_cache = not os.path.exists(cache_zip_path)
        
        if download_to_cache:
            push_log(request_id, f"⬇️ Downloading Forge server ZIP from: {download_url}")
            try:
                download_to_file(download_url, cache_zip_path, request_id)
            except Exception as e:
                error_msg = f"Failed to download Forge server ZIP: {str(e)}. Download manually from: https://files.minecraftforge.net/"
                push_log(request_id, f"❌ {error_msg}")
                raise RuntimeError(json.dumps({
                    "error": "ZIP download failed",
                    "message": str(e),
                    "download_link": "https://files.minecraftforge.net/"
                }))
        else:
            push_log(request_id, f"✅ Using existing cached ZIP file")
        
        # Validate the ZIP
        if not os.path.exists(cache_zip_path):
            error_msg = f"Forge server ZIP was not downloaded. Download manually from: https://files.minecraftforge.net/"
            push_log(request_id, f"❌ {error_msg}")
            raise RuntimeError(json.dumps({
                "error": "ZIP not found",
                "message": "Forge server ZIP download failed.",
                "download_link": "https://files.minecraftforge.net/"
            }))
        
        zip_size = os.path.getsize(cache_zip_path)
        if zip_size < MIN_INSTALLER_SIZE:
            error_msg = f"Forge server ZIP is too small or corrupt ({zip_size} bytes). Download manually from: https://files.minecraftforge.net/"
            push_log(request_id, f"❌ {error_msg}")
            if download_to_cache:
                try:
                    os.remove(cache_zip_path)
                except OSError:
                    pass
            raise RuntimeError(json.dumps({
                "error": "Invalid ZIP",
                "message": "Downloaded Forge server ZIP is corrupt or empty.",
                "download_link": "https://files.minecraftforge.net/"
            }))
        
        # Save to cache if we just downloaded it
        if download_to_cache:
            _save_forge_to_cache(version, cache_zip_path, True, request_id)
        
        # Validate and extract ZIP file
        try:
            with zipfile.ZipFile(cache_zip_path, 'r') as zf:
                if zf.testzip() is not None:
                    error_msg = f"Forge server ZIP is corrupt. Download manually from: https://files.minecraftforge.net/"
                    push_log(request_id, f"❌ {error_msg}")
                    raise RuntimeError(json.dumps({
                        "error": "Corrupt ZIP",
                        "message": "Downloaded Forge server ZIP is corrupt.",
                        "download_link": "https://files.minecraftforge.net/"
                    }))
                
                # Extract ZIP contents to server directory
                push_log(request_id, f"📦 Extracting Forge server ZIP file to server directory...")
                zf.extractall(server_dir)
                push_log(request_id, f"✅ Forge server ZIP extracted successfully")
                
                # Check if server.jar was extracted
                if not os.path.exists(target_server_jar):
                    push_log(request_id, f"📁 server.jar not in root, searching subdirectories...")
                    # Look for server.jar in subdirectories
                    for root, dirs, files in os.walk(server_dir):
                        if 'server.jar' in files:
                            extracted_jar = os.path.join(root, 'server.jar')
                            if extracted_jar != target_server_jar:
                                shutil.move(extracted_jar, target_server_jar)
                                push_log(request_id, f"📁 Moved server.jar to root")
                            break
                
                # Check if lib folder was extracted (Forge uses 'libraries' folder)
                lib_dir = os.path.join(server_dir, "libraries")
                lib_found = False
                
                # Check if libraries folder already exists in root
                if os.path.exists(lib_dir):
                    lib_found = True
                    push_log(request_id, f"📁 Found libraries folder in root")
                else:
                    # Look for lib or libraries folder in subdirectories
                    for root, dirs, files in os.walk(server_dir):
                        # Check if we're in root directory
                        if root == server_dir:
                            # Check root directory for lib/libraries
                            if 'libraries' in dirs:
                                lib_found = True
                                push_log(request_id, f"📁 Found libraries folder in root")
                                break
                            elif 'lib' in dirs:
                                lib_source = os.path.join(root, 'lib')
                                shutil.move(lib_source, lib_dir)
                                push_log(request_id, f"📁 Renamed lib folder to libraries")
                                lib_found = True
                                break
                        else:
                            # Check subdirectories
                            if 'libraries' in dirs:
                                lib_source = os.path.join(root, 'libraries')
                                shutil.move(lib_source, lib_dir)
                                push_log(request_id, f"📁 Moved libraries folder to root")
                                lib_found = True
                                break
                            elif 'lib' in dirs:
                                lib_source = os.path.join(root, 'lib')
                                shutil.move(lib_source, lib_dir)
                                push_log(request_id, f"📁 Moved and renamed lib folder to libraries")
                                lib_found = True
                                break
                
                if lib_found:
                    push_log(request_id, f"✅ Libraries folder ready")
                else:
                    push_log(request_id, f"⚠️ No lib/libraries folder found in ZIP")
                
        except zipfile.BadZipFile:
            error_msg = f"Forge server ZIP is not a valid ZIP file. Download manually from: https://files.minecraftforge.net/"
            push_log(request_id, f"❌ {error_msg}")
            raise RuntimeError(json.dumps({
                "error": "Invalid ZIP format",
                "message": "Downloaded file is not a valid ZIP.",
                "download_link": "https://files.minecraftforge.net/"
            }))
        
        zip_size_mb = zip_size / (1024 * 1024)
        push_log(request_id, f"✅ Downloaded and extracted Forge server ZIP ({zip_size_mb:.2f} MB)")
        push_log(request_id, f"📁 Verifying server.jar exists...")
        if os.path.exists(target_server_jar):
            jar_size = os.path.getsize(target_server_jar)
            jar_size_mb = jar_size / (1024 * 1024)
            push_log(request_id, f"✅ Found server.jar ({jar_size_mb:.2f} MB)")
        else:
            push_log(request_id, f"⚠️ server.jar not found in expected location, checking subdirectories...")
        
    else:
        # Download JAR file to cache first, then copy
        safe_version = version.replace('/', '_').replace('\\', '_')
        cache_jar_path = os.path.join(_forge_cache_dir, f"forge-{safe_version}.jar")
        download_to_cache = not os.path.exists(cache_jar_path)
        
        if download_to_cache:
            push_log(request_id, f"⬇️ Downloading Forge server JAR from: {download_url}")
            try:
                download_to_file(download_url, cache_jar_path, request_id)
            except Exception as e:
                error_msg = f"Failed to download Forge server JAR: {str(e)}. Download manually from: https://files.minecraftforge.net/"
                push_log(request_id, f"❌ {error_msg}")
                raise RuntimeError(json.dumps({
                    "error": "JAR download failed",
                    "message": str(e),
                    "download_link": "https://files.minecraftforge.net/"
                }))
        else:
            push_log(request_id, f"✅ Using existing cached JAR file")
        
        # Validate the cached JAR
        if not os.path.exists(cache_jar_path):
            error_msg = f"Forge server JAR was not downloaded. Download manually from: https://files.minecraftforge.net/"
            push_log(request_id, f"❌ {error_msg}")
            raise RuntimeError(json.dumps({
                "error": "JAR not found",
                "message": "Forge server JAR download failed.",
                "download_link": "https://files.minecraftforge.net/"
            }))
        
        jar_size = os.path.getsize(cache_jar_path)
        if jar_size < MIN_INSTALLER_SIZE:
            error_msg = f"Forge server JAR is too small or corrupt ({jar_size} bytes). Download manually from: https://files.minecraftforge.net/"
            push_log(request_id, f"❌ {error_msg}")
            if download_to_cache:
                try:
                    os.remove(cache_jar_path)
                except OSError:
                    pass
            raise RuntimeError(json.dumps({
                "error": "Invalid JAR",
                "message": "Downloaded Forge server JAR is corrupt or empty.",
                "download_link": "https://files.minecraftforge.net/"
            }))
        
        # Validate it's a valid JAR file
        try:
            with zipfile.ZipFile(cache_jar_path, 'r') as zf:
                if zf.testzip() is not None:
                    error_msg = f"Forge server JAR is corrupt. Download manually from: https://files.minecraftforge.net/"
                    push_log(request_id, f"❌ {error_msg}")
                    if download_to_cache:
                        try:
                            os.remove(cache_jar_path)
                        except OSError:
                            pass
                    raise RuntimeError(json.dumps({
                        "error": "Corrupt JAR",
                        "message": "Downloaded Forge server JAR is corrupt.",
                        "download_link": "https://files.minecraftforge.net/"
                    }))
        except zipfile.BadZipFile:
            error_msg = f"Forge server JAR is not a valid ZIP file. Download manually from: https://files.minecraftforge.net/"
            push_log(request_id, f"❌ {error_msg}")
            if download_to_cache:
                try:
                    os.remove(cache_jar_path)
                except OSError:
                    pass
            raise RuntimeError(json.dumps({
                "error": "Invalid JAR format",
                "message": "Downloaded file is not a valid JAR.",
                "download_link": "https://files.minecraftforge.net/"
            }))
        
        # Save to cache if we just downloaded it
        if download_to_cache:
            _save_forge_to_cache(version, cache_jar_path, False, request_id)
        
        # Copy cached JAR to server directory
        push_log(request_id, f"📋 Copying Forge server JAR to server directory...")
        shutil.copy2(cache_jar_path, target_server_jar)
        jar_size_mb = jar_size / (1024 * 1024)
        push_log(request_id, f"✅ Copied Forge server JAR ({jar_size_mb:.2f} MB)")
        push_log(request_id, f"✅ server.jar is ready")
    
    push_log(request_id, "✅ Forge server setup complete")
    
    # Create eula.txt
    push_log(request_id, "📝 Creating eula.txt file...")
    eula_path = os.path.join(server_dir, "eula.txt")
    with open(eula_path, "w") as f:
        f.write("eula=false\n")
    push_log(request_id, "✅ Created eula.txt with eula=false")


def setup_neoforge(mc_version, loader_version, server_dir, request_id, include_starter_jar=True):
    """
    Setup NeoForge server in the given server_dir.
    Note: server_dir already contains mods/, config/, and other override files from the modpack.
    This function only installs the NeoForge server JAR and creates server.jar.
    """
    # Set request_id in thread-local storage so WebLogHandler can push logs to web interface
    _thread_local.request_id = request_id
    push_log(request_id, f"🔧 Setting up NeoForge for Minecraft {mc_version} with NeoForge {loader_version}...")
    api_url = "https://maven.neoforged.net/api/maven/versions/releases/net/neoforged/neoforge"
    push_log(request_id, f"📡 Fetching NeoForge version list from Maven: {api_url}")
    try:
        resp = requests.get(api_url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        error_msg = f"Failed to fetch NeoForge versions: {str(e)}. Download manually from: https://neoforged.net/"
        push_log(request_id, f"❌ {error_msg}")
        raise RuntimeError(json.dumps({
            "error": "Version fetch failed",
            "message": str(e),
            "download_link": "https://neoforged.net/"
        }))
    push_log(request_id, f"🔍 Searching for NeoForge version matching {loader_version}...")
    matches = [v for v in data["versions"] if v.endswith(loader_version)]
    if not matches:
        error_msg = f"No NeoForge version matching loader '{loader_version}' found. Download manually from: https://neoforged.net/"
        push_log(request_id, f"❌ {error_msg}")
        raise RuntimeError(json.dumps({
            "error": "No matching version",
            "message": f"No NeoForge version matching loader '{loader_version}' found",
            "download_link": "https://neoforged.net/"
        }))
    latest = sorted(matches, key=lambda v: tuple(map(int, re.findall(r"\d+", v))))[-1]
    installer_url = f"https://maven.neoforged.net/releases/net/neoforged/neoforge/{latest}/neoforge-{latest}-installer.jar"
    push_log(request_id, f"✅ Resolved NeoForge installer version: {latest}")
    push_log(request_id, f"⬇️ Downloading NeoForge installer from: {installer_url}")
    installer_path = os.path.join(tempfile.gettempdir(), f"neoforge-installer-{latest}-{request_id}.jar")
    try:
        download_to_file(installer_url, installer_path, request_id)
        push_log(request_id, f"✅ NeoForge installer downloaded successfully")
    except Exception as e:
        error_msg = f"Failed to download NeoForge installer: {str(e)}. Download manually from: {installer_url}"
        push_log(request_id, f"❌ {error_msg}")
        raise RuntimeError(json.dumps({
            "error": "Installer download failed",
            "message": str(e),
            "download_link": installer_url
        }))
    if os.path.getsize(installer_path) < MIN_INSTALLER_SIZE:
        error_msg = f"NeoForge installer is too small or corrupt. Download manually from: {installer_url}"
        push_log(request_id, f"❌ {error_msg}")
        raise RuntimeError(json.dumps({
            "error": "Invalid installer",
            "message": "Downloaded NeoForge installer is corrupt or empty.",
            "download_link": installer_url
        }))
    with zipfile.ZipFile(installer_path) as zf:
        if zf.testzip() is not None:
            error_msg = f"NeoForge installer is corrupt. Download manually from: {installer_url}"
            push_log(request_id, f"❌ {error_msg}")
            raise RuntimeError(json.dumps({
                "error": "Corrupt installer",
                "message": "Downloaded NeoForge installer is corrupt.",
                "download_link": installer_url
            }))
    if not os.access(server_dir, os.W_OK):
        push_log(request_id, f"No write permissions for {server_dir}")
        raise RuntimeError(json.dumps({
            "error": "Permission denied",
            "message": f"Cannot write to server directory: {server_dir}",
            "download_link": installer_url
        }))
    push_log(request_id, f"☕ Resolving Java version for NeoForge {mc_version}...")
    java_version = resolve_java_version("neoforge", mc_version)
    java_path = get_java_path(java_version)
    if not os.path.exists(java_path):
        error_msg = f"Java {java_version} not found at {java_path}. Download manually from: {installer_url}"
        push_log(request_id, f"❌ {error_msg}")
        raise RuntimeError(json.dumps({
            "error": "Java not found",
            "message": f"Java {java_version} not found at {java_path}",
            "download_link": installer_url
        }))
    push_log(request_id, f"☕ Using Java {java_version} at {java_path}")
    push_log(request_id, f"🚀 Starting NeoForge installer process...")
    queue = Queue()
    args = ["--server.jar", "--installServer"]
    push_log(request_id, f"📋 Installer arguments: {' '.join(args)}")
    p = Process(target=run_installer, args=(java_path, installer_path, args, server_dir, request_id, queue))
    p.start()
    push_log(request_id, f"⏳ Waiting for NeoForge installer to complete (this may take several minutes)...")
    
    # Read logs from queue in real-time while process is running
    returncode = None
    stdout = None
    start_time = time.time()
    max_wait_time = NEOFORGE_TIMEOUT
    last_log_time = start_time
    
    while p.is_alive() and (time.time() - start_time) < max_wait_time:
        try:
            # Try to get log messages or result from queue with timeout
            item = queue.get(timeout=1)
            if isinstance(item, tuple) and len(item) == 2:
                if item[0] == "LOG":
                    # This is a log message, push it
                    push_log(request_id, item[1])
                    last_log_time = time.time()
                else:
                    # This is the result tuple (returncode, stdout)
                    returncode, stdout = item
                    break
        except Exception:
            # Queue is empty or timeout, continue waiting
            # Check if process is still alive
            if not p.is_alive():
                break
            # Check if we've had no logs for too long (but less than total timeout)
            if time.time() - last_log_time > 60:  # 1 minute without logs
                push_log(request_id, "⏳ Waiting for installer output...")
                last_log_time = time.time()
            # Small sleep to avoid busy waiting
            time.sleep(0.1)
    
    # Wait for process to finish if still running
    if p.is_alive():
        p.join(timeout=5)
        if p.is_alive():
            p.terminate()
            error_msg = f"NeoForge installer process timed out. Download manually from: {installer_url}"
            push_log(request_id, f"❌ {error_msg}")
            raise RuntimeError(json.dumps({
                "error": "Installer timeout",
                "message": "NeoForge installer process took too long.",
                "download_link": installer_url
            }))
    
    # Get result from queue if we haven't already
    if returncode is None:
        try:
            # Try to get any remaining items from queue
            while True:
                try:
                    item = queue.get(timeout=1)
                    if isinstance(item, tuple) and len(item) == 2:
                        if item[0] == "LOG":
                            push_log(request_id, item[1])
                        else:
                            returncode, stdout = item
                            break
                except Exception:
                    break
        except Exception:
            pass
    
    # If we still don't have a result, try one more time
    if returncode is None:
        try:
            returncode, stdout = queue.get(timeout=5)
        except Exception:
            returncode, stdout = -1, "Failed to get installer output from queue"
    
    push_log(request_id, f"✅ NeoForge installer process completed with exit code {returncode}")
    if stdout:
        push_log(request_id, f"📋 NeoForge installer final output: {stdout[:500]}...")  # Limit output length
    if returncode != 0:
        error_msg = f"NeoForge installer failed with exit code {returncode}. Download manually from: {installer_url}"
        push_log(request_id, f"❌ {error_msg}")
        raise RuntimeError(json.dumps({
            "error": "NeoForge installer failed",
            "message": stdout,
            "download_link": installer_url
        }))
    push_log(request_id, f"📁 Checking server directory for JAR files...")
    dir_contents = os.listdir(server_dir)
    push_log(request_id, f"📂 Server directory contains: {len(dir_contents)} items")
    jar_files = [f for f in dir_contents if f.endswith('.jar') and 'installer' not in f.lower()]
    push_log(request_id, f"🔍 Found {len(jar_files)} potential server JAR file(s)")
    if not jar_files:
        push_log(request_id, "Failed to find NeoForge server JAR files.")
        jar_files = [f for f in os.listdir(server_dir) if f.endswith('.jar') and 'installer' not in f.lower()]
        if not jar_files:
            error_msg = f"No JAR files found. Download manually from: {installer_url}"
            push_log(request_id, f"❌ {error_msg}")
            raise RuntimeError(json.dumps({
                "error": "No JAR files found",
                "message": "NeoForge setup failed to generate a server JAR.",
                "download_link": installer_url
            }))
    if len(jar_files) < 1:
        error_msg = f"Expected at least one JAR file, but found {len(jar_files)}. Download manually from: {installer_url}"
        push_log(request_id, f"❌ {error_msg}")
        raise RuntimeError(json.dumps({
            "error": "No valid JAR files found",
            "message": "NeoForge setup failed to generate a valid server JAR.",
            "download_link": installer_url
        }))
    push_log(request_id, f"Found {len(jar_files)} JAR files: {', '.join(jar_files)}")
    
    # Prioritize server JAR files - look for actual server JAR, not launcher or installer
    server_jar_candidates = []
    for jar_file in jar_files:
        jar_lower = jar_file.lower()
        # Skip launcher and installer JARs
        if 'launch' in jar_lower or 'installer' in jar_lower or 'client' in jar_lower:
            continue
        # Prioritize files with "server" in name, or neoforge server files
        if 'server' in jar_lower or 'neoforge' in jar_lower:
            server_jar_candidates.insert(0, jar_file)
        else:
            server_jar_candidates.append(jar_file)
    
    # Use prioritized list or fall back to original list
    if server_jar_candidates:
        target_jar = server_jar_candidates[0]
    elif jar_files:
        target_jar = jar_files[0]
    else:
        target_jar = None
    
    if not target_jar:
        error_msg = f"NeoForge setup failed: No valid server JAR found. Download manually from: {installer_url}"
        push_log(request_id, f"❌ {error_msg}")
        raise RuntimeError(json.dumps({
            "error": "server.jar not found",
            "message": "NeoForge setup failed: No valid server JAR found.",
            "download_link": installer_url
        }))
    
    if len(jar_files) > 1:
        push_log(request_id, f"Multiple JAR files found: {', '.join(jar_files)}. Using {target_jar}.")
    
    server_jar = os.path.join(server_dir, target_jar)
    target_server_jar = os.path.join(server_dir, "server.jar")
    
    if os.path.exists(server_jar) and target_jar != "server.jar":
        push_log(request_id, f"📝 Renaming {target_jar} to server.jar...")
        if os.path.exists(target_server_jar):
            os.remove(target_server_jar)  # Remove old server.jar if it exists
        try:
            # Try rename first (faster if same filesystem)
            os.rename(server_jar, target_server_jar)
            push_log(request_id, f"✅ Renamed {target_jar} to server.jar")
        except OSError:
            # If rename fails (different filesystem), use copy
            shutil.copy2(server_jar, target_server_jar)
            push_log(request_id, f"✅ Copied {target_jar} to server.jar")
    elif os.path.exists(target_server_jar):
        push_log(request_id, f"✅ server.jar already exists and is ready")
    elif not os.path.exists(target_server_jar):
        error_msg = f"NeoForge setup failed: server.jar not found. Download manually from: {installer_url}"
        push_log(request_id, f"❌ {error_msg}")
        raise RuntimeError(json.dumps({
            "error": "server.jar not found",
            "message": f"NeoForge setup failed: server.jar was not created.",
            "download_link": installer_url
        }))
    push_log(request_id, "✅ NeoForge server setup complete")
    push_log(request_id, "📝 Creating eula.txt file...")
    eula_path = os.path.join(server_dir, "eula.txt")
    with open(eula_path, "w") as f:
        f.write("eula=false\n")
    push_log(request_id, "✅ Created eula.txt with eula=false")
    push_log(request_id, f"🧹 Cleaning up NeoForge installer...")
    os.remove(installer_path)
    push_log(request_id, f"✅ Deleted NeoForge installer: {installer_path}")


def download_vanilla_server_jar(mc_version, server_dir, request_id):
    """Download vanilla Minecraft server JAR for the specified version."""
    # Set request_id in thread-local storage so WebLogHandler can push logs to web interface
    if request_id:
        _thread_local.request_id = request_id
    minecraft_jar_path = os.path.join(server_dir, "minecraft.jar")
    
    # Check cache first
    cache_jar_path = os.path.join(_forge_cache_dir, f"vanilla-{mc_version}.jar")
    if os.path.exists(cache_jar_path):
        push_log(request_id, f"✅ Using cached vanilla server JAR for {mc_version}")
        shutil.copy2(cache_jar_path, minecraft_jar_path)
        return
    
    push_log(request_id, f"⬇️ Downloading vanilla Minecraft server JAR for {mc_version}...")
    session = get_http_session()
    
    try:
        # Get version manifest
        manifest_url = "https://launchermeta.mojang.com/mc/game/version_manifest.json"
        push_log(request_id, f"📡 Fetching version manifest...")
        manifest_resp = session.get(manifest_url, timeout=15)
        manifest_resp.raise_for_status()
        manifest = manifest_resp.json()
        
        # Find the version
        version_info = None
        for version_entry in manifest.get('versions', []):
            if version_entry.get('id') == mc_version:
                version_info = version_entry
                break
        
        if not version_info:
            # Try to find a close match (e.g., 1.20.1 might be listed as 1.20.1)
            for version_entry in manifest.get('versions', []):
                version_id = version_entry.get('id', '')
                if version_id.startswith(mc_version) or mc_version.startswith(version_id.split('.')[0]):
                    version_info = version_entry
                    push_log(request_id, f"⚠️ Using closest match: {version_id} for {mc_version}")
                    break
        
        if not version_info:
            raise ValueError(f"Version {mc_version} not found in manifest")
        
        # Get version details
        version_url = version_info.get('url')
        if not version_url:
            raise ValueError(f"No URL found for version {mc_version}")
        
        push_log(request_id, f"📡 Fetching version details from: {version_url}")
        version_resp = session.get(version_url, timeout=15)
        version_resp.raise_for_status()
        version_data = version_resp.json()
        
        # Get server JAR download URL
        server_downloads = version_data.get('downloads', {}).get('server')
        if not server_downloads:
            raise ValueError(f"No server download found for version {mc_version}")
        
        server_url = server_downloads.get('url')
        if not server_url:
            raise ValueError(f"No server JAR URL found for version {mc_version}")
        
        # Download vanilla server JAR
        push_log(request_id, f"⬇️ Downloading vanilla server JAR from Mojang...")
        download_to_file(server_url, cache_jar_path, request_id)
        
        # Copy to server directory
        shutil.copy2(cache_jar_path, minecraft_jar_path)
        jar_size = os.path.getsize(minecraft_jar_path)
        jar_size_mb = jar_size / (1024 * 1024)
        push_log(request_id, f"✅ Downloaded vanilla server JAR ({jar_size_mb:.2f} MB)")
        
    except Exception as e:
        error_msg = f"Failed to download vanilla server JAR: {str(e)}"
        push_log(request_id, f"❌ {error_msg}")
        raise RuntimeError(json.dumps({
            "error": "Vanilla server JAR download failed",
            "message": str(e),
            "download_link": "https://www.minecraft.net/en-us/download/server"
        }))


def setup_quilt(mc_version, loader_version, server_dir, request_id):
    """
    Setup Quilt server using mcjars.app API to download server JAR directly.
    Similar to Forge setup, optimized for high concurrency.
    Quilt requires the vanilla Minecraft server JAR to be present as minecraft.jar.
    """
    # Set request_id in thread-local storage so WebLogHandler can push logs to web interface
    _thread_local.request_id = request_id
    version = f"{mc_version}-{loader_version}"
    push_log(request_id, f"🔍 Setting up Quilt {version}...")
    api_base_url = "https://mcjars.app/api"
    
    if not os.access(server_dir, os.W_OK):
        push_log(request_id, f"No write permissions for {server_dir}")
        raise RuntimeError(json.dumps({
            "error": "Permission denied",
            "message": f"Cannot write to server directory: {server_dir}",
            "download_link": "https://quiltmc.org/install"
        }))
    
    # Query mcjars.app API for Quilt builds (NOT Fabric)
    push_log(request_id, f"🧵 Setting up QUILT server (not Fabric) for {version}")
    push_log(request_id, f"🔍 Querying mcjars.app API for Quilt {version}")
    download_url = None
    
    try:
        # Query /api/v1/builds/QUILT/{mc_version} to get all builds for that MC version
        # Using QUILT loader type, NOT FABRIC
        builds_url = f"{api_base_url}/v1/builds/QUILT/{mc_version}"
        push_log(request_id, f"📡 Fetching Quilt builds from: {builds_url}")
        session = get_http_session()
        resp = session.get(builds_url, timeout=15)
        
        if resp.status_code == 404:
            # Try with "latest" build endpoint
            latest_url = f"{api_base_url}/v1/builds/QUILT/{mc_version}/latest"
            push_log(request_id, f"📡 Trying latest Quilt build: {latest_url}")
            resp = session.get(latest_url, timeout=15)
        
        resp.raise_for_status()
        api_response = resp.json()
        
        # Handle API response structure: {"success": true, "builds": [...]}
        if not api_response.get('success'):
            raise ValueError("API returned unsuccessful response")
        
        # Get builds list - could be direct array or nested in response
        if 'build' in api_response:
            # Single build response from /latest endpoint
            builds_list = [api_response['build']]
        elif 'builds' in api_response:
            builds_list = api_response['builds']
        elif isinstance(api_response, list):
            builds_list = api_response
        else:
            builds_list = [api_response]
        
        if not builds_list or len(builds_list) == 0:
            raise ValueError(f"No builds found for Quilt {version}")
        
        # Find the build matching the Quilt loader version
        # Ensure we're getting Quilt builds, not Fabric
        build = None
        for b in builds_list:
            project_version = b.get('projectVersionId') or b.get('name') or ''
            loader_name = b.get('loader', '').upper() if isinstance(b.get('loader'), str) else ''
            
            # Verify this is actually a Quilt build (not Fabric)
            if 'FABRIC' in loader_name or 'fabric' in str(project_version).lower():
                push_log(request_id, f"⚠️ Skipping Fabric build: {project_version}")
                continue
            
            # Check if Quilt loader version matches
            if loader_version in str(project_version) or str(project_version) in loader_version:
                build = b
                push_log(request_id, f"✅ Found matching Quilt build: {project_version}")
                break
        
        # If no exact match, use the first build (but verify it's Quilt)
        if not build:
            push_log(request_id, f"⚠️ No exact match found, using first available Quilt build")
            build = builds_list[0]
            # Double-check it's not Fabric
            loader_name = build.get('loader', '').upper() if isinstance(build.get('loader'), str) else ''
            if 'FABRIC' in loader_name:
                push_log(request_id, f"⚠️ Warning: First build appears to be Fabric, but using it anyway")
        
        # Prefer zipUrl over jarUrl (ZIP contains server.jar and lib folder)
        download_url = build.get('zipUrl')
        is_zip = True
        
        if not download_url:
            # Try jarUrl as fallback
            download_url = build.get('jarUrl')
            is_zip = False
        
        if not download_url:
            # Try alternative field names
            download_url = (build.get('zip_url') or build.get('jar_url') or 
                          build.get('download') or build.get('url') or build.get('downloadUrl'))
            is_zip = 'zip' in str(download_url).lower() if download_url else False
        
        # If zipUrl/jarUrl is null, check installation array
        if not download_url and 'installation' in build:
            installation = build['installation']
            push_log(request_id, f"🔍 Checking installation array from build (length: {len(installation) if isinstance(installation, list) else 'N/A'})")
            download_url = extract_jar_url_from_installation(installation, request_id)
            is_zip = download_url and (download_url.lower().endswith('.zip') or '.zip' in download_url.lower()) if download_url else False
        
        # If still no download URL, try to get build details using build ID
        if not download_url:
            build_id = build.get('id')
            if build_id:
                push_log(request_id, f"📦 Fetching build details for build ID: {build_id}")
                build_url = f"{api_base_url}/v1/build/{build_id}"
                build_resp = session.get(build_url, timeout=15)
                build_resp.raise_for_status()
                build_info = build_resp.json()
                if build_info.get('success') and 'build' in build_info:
                    build_data = build_info['build']
                    # Prefer zipUrl
                    download_url = build_data.get('zipUrl')
                    is_zip = True
                    
                    if not download_url:
                        download_url = build_data.get('jarUrl') or build_data.get('jar_url')
                        is_zip = False
                    
                    # If still no zipUrl/jarUrl, check installation array from build details
                    if not download_url and 'installation' in build_data:
                        installation = build_data['installation']
                        push_log(request_id, f"🔍 Checking installation array from build details (length: {len(installation) if isinstance(installation, list) else 'N/A'})")
                        download_url = extract_jar_url_from_installation(installation, request_id)
                        is_zip = download_url and (download_url.lower().endswith('.zip') or '.zip' in download_url.lower()) if download_url else False
        
        if not download_url:
            raise ValueError("No zipUrl, jarUrl or installation download URL found in API response")
        
        if is_zip:
            push_log(request_id, f"✅ Found Quilt server ZIP download URL (Quilt loader, not Fabric)")
        else:
            push_log(request_id, f"✅ Found Quilt server JAR download URL (Quilt loader, not Fabric)")
        
    except requests.exceptions.RequestException as e:
        error_msg = f"Failed to query mcjars.app API: {str(e)}. Download manually from: https://quiltmc.org/install"
        push_log(request_id, f"❌ {error_msg}")
        raise RuntimeError(json.dumps({
            "error": "API query failed",
            "message": str(e),
            "download_link": "https://quiltmc.org/install"
        }))
    except (KeyError, ValueError, TypeError) as e:
        error_msg = f"Invalid response from mcjars.app API: {str(e)}. Download manually from: https://quiltmc.org/install"
        push_log(request_id, f"❌ {error_msg}")
        raise RuntimeError(json.dumps({
            "error": "Invalid API response",
            "message": str(e),
            "download_link": "https://quiltmc.org/install"
        }))
    
    # Download the ZIP or JAR file (similar to Forge)
    target_server_jar = os.path.join(server_dir, "server.jar")
    
    if is_zip:
        # Download ZIP file to cache location first, then copy/extract
        safe_version = version.replace('/', '_').replace('\\', '_')
        cache_zip_path = os.path.join(_forge_cache_dir, f"quilt-{safe_version}.zip")
        download_to_cache = not os.path.exists(cache_zip_path)
        
        if download_to_cache:
            push_log(request_id, f"⬇️ Downloading Quilt server ZIP from: {download_url}")
            try:
                download_to_file(download_url, cache_zip_path, request_id)
            except Exception as e:
                error_msg = f"Failed to download Quilt server ZIP: {str(e)}. Download manually from: https://quiltmc.org/install"
                push_log(request_id, f"❌ {error_msg}")
                raise RuntimeError(json.dumps({
                    "error": "ZIP download failed",
                    "message": str(e),
                    "download_link": "https://quiltmc.org/install"
                }))
        else:
            push_log(request_id, f"✅ Using existing cached ZIP file")
        
        # Validate the ZIP
        if not os.path.exists(cache_zip_path):
            error_msg = f"Quilt server ZIP was not downloaded. Download manually from: https://quiltmc.org/install"
            push_log(request_id, f"❌ {error_msg}")
            raise RuntimeError(json.dumps({
                "error": "ZIP not found",
                "message": "Quilt server ZIP download failed.",
                "download_link": "https://quiltmc.org/install"
            }))
        
        zip_size = os.path.getsize(cache_zip_path)
        if zip_size < MIN_INSTALLER_SIZE:
            error_msg = f"Quilt server ZIP is too small or corrupt ({zip_size} bytes). Download manually from: https://quiltmc.org/install"
            push_log(request_id, f"❌ {error_msg}")
            if download_to_cache:
                try:
                    os.remove(cache_zip_path)
                except OSError:
                    pass
            raise RuntimeError(json.dumps({
                "error": "Invalid ZIP",
                "message": "Downloaded Quilt server ZIP is corrupt or empty.",
                "download_link": "https://quiltmc.org/install"
            }))
        
        # Save to cache if we just downloaded it
        if download_to_cache:
            _save_forge_to_cache(version, cache_zip_path, True, request_id)
        
        # Validate and extract ZIP file
        try:
            with zipfile.ZipFile(cache_zip_path, 'r') as zf:
                if zf.testzip() is not None:
                    error_msg = f"Quilt server ZIP is corrupt. Download manually from: https://quiltmc.org/install"
                    push_log(request_id, f"❌ {error_msg}")
                    raise RuntimeError(json.dumps({
                        "error": "Corrupt ZIP",
                        "message": "Downloaded Quilt server ZIP is corrupt.",
                        "download_link": "https://quiltmc.org/install"
                    }))
                
                # Extract ZIP contents to server directory
                push_log(request_id, f"📦 Extracting Quilt server ZIP file to server directory...")
                zf.extractall(server_dir)
                push_log(request_id, f"✅ Quilt server ZIP extracted successfully")
                
                # Check if server.jar was extracted
                if not os.path.exists(target_server_jar):
                    push_log(request_id, f"📁 server.jar not in root, searching subdirectories...")
                    # Look for server.jar in subdirectories
                    for root, dirs, files in os.walk(server_dir):
                        if 'server.jar' in files:
                            extracted_jar = os.path.join(root, 'server.jar')
                            if extracted_jar != target_server_jar:
                                shutil.move(extracted_jar, target_server_jar)
                                push_log(request_id, f"📁 Moved server.jar to root")
                            break
                
        except zipfile.BadZipFile:
            error_msg = f"Quilt server ZIP is not a valid ZIP file. Download manually from: https://quiltmc.org/install"
            push_log(request_id, f"❌ {error_msg}")
            raise RuntimeError(json.dumps({
                "error": "Invalid ZIP format",
                "message": "Downloaded file is not a valid ZIP.",
                "download_link": "https://quiltmc.org/install"
            }))
        
        zip_size_mb = zip_size / (1024 * 1024)
        push_log(request_id, f"✅ Downloaded and extracted Quilt server ZIP ({zip_size_mb:.2f} MB)")
        push_log(request_id, f"📁 Verifying server.jar exists...")
        if os.path.exists(target_server_jar):
            jar_size = os.path.getsize(target_server_jar)
            jar_size_mb = jar_size / (1024 * 1024)
            push_log(request_id, f"✅ Found server.jar ({jar_size_mb:.2f} MB)")
        else:
            push_log(request_id, f"⚠️ server.jar not found in expected location, checking subdirectories...")
        
    else:
        # Download JAR file to cache first, then copy
        safe_version = version.replace('/', '_').replace('\\', '_')
        cache_jar_path = os.path.join(_forge_cache_dir, f"quilt-{safe_version}.jar")
        download_to_cache = not os.path.exists(cache_jar_path)
        
        if download_to_cache:
            push_log(request_id, f"⬇️ Downloading Quilt server JAR from: {download_url}")
            try:
                download_to_file(download_url, cache_jar_path, request_id)
            except Exception as e:
                error_msg = f"Failed to download Quilt server JAR: {str(e)}. Download manually from: https://quiltmc.org/install"
                push_log(request_id, f"❌ {error_msg}")
                raise RuntimeError(json.dumps({
                    "error": "JAR download failed",
                    "message": str(e),
                    "download_link": "https://quiltmc.org/install"
                }))
        else:
            push_log(request_id, f"✅ Using existing cached JAR file")
        
        # Validate the cached JAR
        if not os.path.exists(cache_jar_path):
            error_msg = f"Quilt server JAR was not downloaded. Download manually from: https://quiltmc.org/install"
            push_log(request_id, f"❌ {error_msg}")
            raise RuntimeError(json.dumps({
                "error": "JAR not found",
                "message": "Quilt server JAR download failed.",
                "download_link": "https://quiltmc.org/install"
            }))
        
        jar_size = os.path.getsize(cache_jar_path)
        if jar_size < MIN_INSTALLER_SIZE:
            error_msg = f"Quilt server JAR is too small or corrupt ({jar_size} bytes). Download manually from: https://quiltmc.org/install"
            push_log(request_id, f"❌ {error_msg}")
            if download_to_cache:
                try:
                    os.remove(cache_jar_path)
                except OSError:
                    pass
            raise RuntimeError(json.dumps({
                "error": "Invalid JAR",
                "message": "Downloaded Quilt server JAR is corrupt or empty.",
                "download_link": "https://quiltmc.org/install"
            }))
        
        # Validate it's a valid JAR file
        try:
            with zipfile.ZipFile(cache_jar_path, 'r') as zf:
                if zf.testzip() is not None:
                    error_msg = f"Quilt server JAR is corrupt. Download manually from: https://quiltmc.org/install"
                    push_log(request_id, f"❌ {error_msg}")
                    if download_to_cache:
                        try:
                            os.remove(cache_jar_path)
                        except OSError:
                            pass
                    raise RuntimeError(json.dumps({
                        "error": "Corrupt JAR",
                        "message": "Downloaded Quilt server JAR is corrupt.",
                        "download_link": "https://quiltmc.org/install"
                    }))
        except zipfile.BadZipFile:
            error_msg = f"Quilt server JAR is not a valid ZIP file. Download manually from: https://quiltmc.org/install"
            push_log(request_id, f"❌ {error_msg}")
            if download_to_cache:
                try:
                    os.remove(cache_jar_path)
                except OSError:
                    pass
            raise RuntimeError(json.dumps({
                "error": "Invalid JAR format",
                "message": "Downloaded file is not a valid JAR.",
                "download_link": "https://quiltmc.org/install"
            }))
        
        # Save to cache if we just downloaded it
        if download_to_cache:
            _save_forge_to_cache(version, cache_jar_path, False, request_id)
        
        # Copy cached JAR to server directory
        push_log(request_id, f"📋 Copying Quilt server JAR to server directory...")
        shutil.copy2(cache_jar_path, target_server_jar)
        jar_size_mb = jar_size / (1024 * 1024)
        push_log(request_id, f"✅ Copied Quilt server JAR ({jar_size_mb:.2f} MB)")
        push_log(request_id, f"✅ server.jar is ready")
    
    # Quilt requires the vanilla Minecraft server JAR to be present
    minecraft_jar_path = os.path.join(server_dir, "minecraft.jar")
    if not os.path.exists(minecraft_jar_path):
        push_log(request_id, "📦 Quilt requires vanilla Minecraft server JAR, downloading...")
        try:
            download_vanilla_server_jar(mc_version, server_dir, request_id)
            push_log(request_id, f"✅ Vanilla Minecraft server JAR downloaded successfully")
        except Exception as e:
            push_log(request_id, f"❌ Failed to download vanilla server JAR: {e}")
            # Don't fail completely, but warn the user
            push_log(request_id, f"⚠️ Warning: Quilt may not start without minecraft.jar")
    else:
        push_log(request_id, f"✅ Vanilla Minecraft server JAR already exists")
    
    # Create quilt-server-launcher.properties if it doesn't exist
    push_log(request_id, "📝 Creating quilt-server-launcher.properties...")
    launcher_props_path = os.path.join(server_dir, "quilt-server-launcher.properties")
    if not os.path.exists(launcher_props_path):
        with open(launcher_props_path, "w") as f:
            f.write(f"serverJar=minecraft.jar\n")
        push_log(request_id, "✅ Created quilt-server-launcher.properties")
    else:
        push_log(request_id, "✅ quilt-server-launcher.properties already exists")
    
    push_log(request_id, "✅ Quilt server setup complete")
    
    # Create eula.txt
    push_log(request_id, "📝 Creating eula.txt file...")
    eula_path = os.path.join(server_dir, "eula.txt")
    with open(eula_path, "w") as f:
        f.write("eula=false\n")
    push_log(request_id, "✅ Created eula.txt with eula=false")


if __name__ == '__main__':
    # Mark as running locally for logging purposes
    os.environ["RUNNING_LOCALLY"] = "1"
    
    try:
        subprocess.run(["java", "-version"], check=True, capture_output=True)
    except Exception as e:
        logging.warning("Default Java not available or not installed.")
    
    # Initialize server count
    initialize_server_count()
    
    # Load admin logs from file on startup
    _load_admin_logs_from_file()
    
    # Flush any pending admin logs on startup
    _flush_admin_logs_to_file()
    
    # Register enhanced cleanup function to flush logs on shutdown
    def enhanced_shutdown(signum, frame):
        logging.info(f"Received signal {signum}, shutting down gracefully...")
        save_server_count()
        _flush_admin_logs_to_file()
        sys.exit(0)
    
    # Override signal handlers with enhanced version
    signal.signal(signal.SIGTERM, enhanced_shutdown)
    signal.signal(signal.SIGINT, enhanced_shutdown)
    
    # Also register atexit for additional cleanup
    def flush_logs_on_exit():
        _flush_admin_logs_to_file()
    atexit.register(flush_logs_on_exit)
    
    # Use PORT from environment (Render.com provides this, default to 8090 for local)
    port = int(os.environ.get("PORT", 8090))
    host = os.environ.get("HOST", "0.0.0.0")
    debug_mode = os.environ.get("DEBUG", "False").lower() == "true"
    
    logging.info(f"🚀 Starting server on {host}:{port} (debug={debug_mode})")
    logging.info(f"📁 Using storage: {PERSISTENT_TEMP_ROOT}")
    logging.info(f"💾 Count file: {COUNT_FILE}")
    
    socketio.run(app, debug=debug_mode, port=port, host=host, allow_unsafe_werkzeug=True)   