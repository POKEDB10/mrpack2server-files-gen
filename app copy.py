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
from io import BytesIO
from multiprocessing import Process, Queue
from threading import Lock

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
from flask_socketio import SocketIO, emit

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

# Initialize logging - only from primary worker to reduce clutter
if os.environ.get("PRIMARY_WORKER") == "1":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    logging.info("🛠 Starting Minecraft Server File Generator (MSFG)")
    log_installed_java_versions()
else:
    # Other workers: minimal logging setup
    logging.basicConfig(
        level=logging.WARNING,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )

# Global state
log_buffers = defaultdict(list)
log_locks = defaultdict(Lock)
generated_server_count = 0
generated_server_lock = Lock()
download_executor = ThreadPoolExecutor(max_workers=MAX_WORKERS_DOWNLOAD)
copy_executor = ThreadPoolExecutor(max_workers=MAX_WORKERS_COPY)
active_users = set()
# Use a writable location for the count file (prefer current directory, fallback to temp)
def get_writable_count_file_dir():
    """Get a writable directory for the count file."""
    # Try current directory first
    try:
        test_dir = os.getcwd()
        test_file = os.path.join(test_dir, ".test_write")
        # Try to create and delete a test file
        with open(test_file, 'w') as f:
            f.write('test')
        os.remove(test_file)
        return test_dir
    except (OSError, IOError, PermissionError):
        pass
    
    # Fallback to temp directory
    try:
        temp_dir = tempfile.gettempdir()
        test_file = os.path.join(temp_dir, ".test_write")
        with open(test_file, 'w') as f:
            f.write('test')
        os.remove(test_file)
        return temp_dir
    except (OSError, IOError, PermissionError):
        # Last resort: use temp directory anyway
        return tempfile.gettempdir()

_count_file_dir = get_writable_count_file_dir()
COUNT_FILE = os.path.join(_count_file_dir, "generated_server_count.txt")
recent_ips = deque(maxlen=100)
access_log = deque(maxlen=200)
socketio = SocketIO(app, async_mode='gevent')
logging.getLogger('engineio').setLevel(logging.WARNING)
logging.getLogger('socketio').setLevel(logging.WARNING)

# Directories
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

# Only initialize count on primary worker to reduce log clutter
if os.environ.get("PRIMARY_WORKER") == "1":
    try:
        if os.path.exists(COUNT_FILE):
            with open(COUNT_FILE, "r") as f:
                generated_server_count = int(f.read().strip())
                logging.info(f"✅ Loaded generated server count: {generated_server_count}")
    except Exception as e:
        # Initialize the count file if it doesn't exist or is empty
        if not os.path.exists(COUNT_FILE) or os.path.getsize(COUNT_FILE) == 0:
            with open(COUNT_FILE, "w") as f:
                f.write("0")
            logging.info("✅ Initialized server count file with 0")


def initialize_server_count():
    """Initialize the server count from file."""
    global generated_server_count
    try:
        # Ensure directory exists
        count_dir = os.path.dirname(COUNT_FILE) or os.getcwd()
        os.makedirs(count_dir, exist_ok=True)
        
        # Check if the directory is writable
        if not os.access(count_dir, os.W_OK):
            raise PermissionError(f"No write permissions for directory: {count_dir}")
        
        if os.path.exists(COUNT_FILE):
            with open(COUNT_FILE, "r") as f:
                with portalocker.Lock(f, timeout=LOCK_TIMEOUT):
                    content = f.read().strip()
                    generated_server_count = int(content) if content else 0
                    # Only log from primary worker to reduce clutter
                    if os.environ.get("PRIMARY_WORKER") == "1":
                        logging.info(f"✅ Loaded generated server count: {generated_server_count}")
        else:
            # File doesn't exist, create it
            with open(COUNT_FILE, "w") as f:
                with portalocker.Lock(f, timeout=LOCK_TIMEOUT):
                    f.write("0")
                    f.flush()
                    os.fsync(f.fileno())  # Ensure data is written to disk
            generated_server_count = 0
            # Only log from primary worker to reduce clutter
            if os.environ.get("PRIMARY_WORKER") == "1":
                logging.info("✅ Initialized server count file with 0")
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
    for attempt in range(max_retries):
        try:
            # Ensure directory exists and is writable
            count_dir = os.path.dirname(COUNT_FILE) or tempfile.gettempdir()
            try:
                os.makedirs(count_dir, exist_ok=True)
            except (OSError, PermissionError):
                # If directory creation fails, try temp directory
                count_dir = tempfile.gettempdir()
                COUNT_FILE = os.path.join(count_dir, "generated_server_count.txt")
                os.makedirs(count_dir, exist_ok=True)
            
            # Verify directory is writable by trying to create a test file
            try:
                test_file = os.path.join(count_dir, ".test_write")
                with open(test_file, 'w') as f:
                    f.write('test')
                os.remove(test_file)
            except (OSError, IOError, PermissionError):
                # Directory not writable, use temp directory
                count_dir = tempfile.gettempdir()
                COUNT_FILE = os.path.join(count_dir, "generated_server_count.txt")
            
            # Use file lock to ensure atomic read-modify-write
            # Always use 'w+' mode to create file if it doesn't exist
            file_exists = os.path.exists(COUNT_FILE)
            
            # Open file - use 'w+' if it doesn't exist, 'r+' if it does
            mode = "r+" if file_exists else "w+"
            try:
                with open(COUNT_FILE, mode) as f:
                    with portalocker.Lock(f, timeout=LOCK_TIMEOUT):
                        if file_exists:
                            content = f.read().strip()
                            current_count = int(content) if content else 0
                        else:
                            current_count = 0
                        
                        new_count = current_count + 1
                        f.seek(0)
                        f.truncate()
                        f.write(str(new_count))
                        f.flush()
                        os.fsync(f.fileno())  # Ensure data is written to disk
                        generated_server_count = new_count
            except (FileNotFoundError, OSError, IOError) as e:
                # File was deleted or directory issue, retry with temp directory
                if attempt < max_retries - 1:
                    count_dir = tempfile.gettempdir()
                    COUNT_FILE = os.path.join(count_dir, "generated_server_count.txt")
                    time.sleep(0.5)
                    continue
                raise
            
            # Only log from primary worker to reduce clutter
            if os.environ.get("PRIMARY_WORKER") == "1":
                logging.info(f"✅ Incremented server count to: {new_count}")
            return new_count
        except PermissionError as e:
            if os.environ.get("PRIMARY_WORKER") == "1":
                logging.error(f"❌ Failed to increment server count due to permissions: {e}")
            return generated_server_count
        except portalocker.LockException as e:
            if attempt < max_retries - 1:
                time.sleep(0.5)  # Wait before retrying
                continue
            if os.environ.get("PRIMARY_WORKER") == "1":
                logging.error(f"❌ Max retries reached for incrementing server count: {e}")
            return generated_server_count
        except (FileNotFoundError, OSError, IOError) as e:
            # File was deleted between check and open, or directory doesn't exist, retry
            if attempt < max_retries - 1:
                # Try to use temp directory as fallback
                try:
                    count_dir = tempfile.gettempdir()
                    COUNT_FILE = os.path.join(count_dir, "generated_server_count.txt")
                    os.makedirs(count_dir, exist_ok=True)
                except:
                    pass
                time.sleep(0.5)
                continue
            if os.environ.get("PRIMARY_WORKER") == "1":
                logging.error(f"❌ Max retries reached: file not found - {e}")
            return generated_server_count
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(0.5)  # Wait before retrying
                continue
            if os.environ.get("PRIMARY_WORKER") == "1":
                logging.error(f"❌ Max retries reached for incrementing server count: {e}")
            return generated_server_count

def save_server_count():
    """Save the current server count to file on shutdown, non-blocking attempt."""
    try:
        # Ensure directory exists
        count_dir = os.path.dirname(COUNT_FILE) or os.getcwd()
        os.makedirs(count_dir, exist_ok=True)
        
        # Check if the directory is writable
        if not os.access(count_dir, os.W_OK):
            raise PermissionError(f"No write permissions for directory: {count_dir}")
        
        # Try to acquire the lock non-blocking
        with open(COUNT_FILE, "w") as f:
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
            logging.error(f"❌ Failed to save server count on exit: {e}")


# Also add signal handlers for graceful shutdown
def handle_shutdown(signum, frame):
    logging.info(f"Received signal {signum}, shutting down gracefully...")
    save_server_count()
    sys.exit(0)

signal.signal(signal.SIGINT, handle_shutdown)
signal.signal(signal.SIGTERM, handle_shutdown)
atexit.register(save_server_count)


def require_admin(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin_login", next=request.path))
        return view_func(*args, **kwargs)
    return wrapped


USERS_FILE = os.path.join(os.getcwd(), "config", "users.json")

os.makedirs(os.path.dirname(USERS_FILE), exist_ok=True)

if not os.path.exists(USERS_FILE):
    default_users = {
        "pokedb": bcrypt.hashpw(b"WwAaSsDd@1999", bcrypt.gensalt()).decode(),
        "Alino6829": bcrypt.hashpw(b"5CY&zhqU26rcbX", bcrypt.gensalt()).decode()
    }
    with open(USERS_FILE, "w") as f:
        json.dump(default_users, f)
    os.chmod(USERS_FILE, 0o600)

def load_admin_users():
    try:
        with open(USERS_FILE, "r") as f:
            users = json.load(f)
        return {k: v.encode() for k, v in users.items()}  
    except Exception as e:
        logging.error(f"Failed to load admin users: {e}")
        return {}

ADMIN_USERS = load_admin_users()


@app.before_request
def track_active_users():
    ip = request.remote_addr
    session['ip'] = ip
    active_users.add(ip)

    entry = {
        "ip": ip,
        "path": request.path,
        "time": datetime.datetime.now().isoformat()
    }
    recent_ips.appendleft(entry)
    access_log.appendleft(entry)


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
        
        # Clean up request tracking
        if request_id in log_locks:
            with download_status_lock:
                if request_id in download_status:
                    del download_status[request_id]
            with log_locks[request_id]:
                if request_id in log_buffers:
                    del log_buffers[request_id]
            # Remove lock last
            del log_locks[request_id]
    except Exception as e:
        logging.warning(f"[{request_id}] ⚠️ Cleanup failed: {e}")
        
        
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if session.get("is_admin"):
        return redirect(url_for("admin_dashboard"))  # Redirect if already logged in
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password", "").encode()
        ip = request.remote_addr
        logging.info(f"Login attempt: username={username}, ip={ip}")
        if not username or not password:
            logging.warning(f"Missing username or password: username={username}, ip={ip}")
            return render_template("admin_login.html", error="Username and password are required")
        hashed = ADMIN_USERS.get(username)
        if hashed and bcrypt.checkpw(password, hashed):
            session["is_admin"] = True
            session["admin_user"] = username
            session.permanent = True
            logging.info(f"Login successful: username={username}, ip={ip}")
            return redirect(url_for("admin_dashboard"))
        logging.warning(f"Login failed: username={username}, ip={ip}")
        return render_template("admin_login.html", error="Invalid username or password")
    return render_template("admin_login.html", error=None)


@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))


@app.route("/admin/logs")
@require_admin
def view_logs():
    seen = set()
    unique_logs = []
    for entry in access_log:
        key = (entry["ip"], entry["path"], entry["time"])
        if key not in seen:
            seen.add(key)
            unique_logs.append(entry)

    return jsonify({
        "recent_ips": list(recent_ips),
        "access_log": unique_logs
    })


@app.route("/admin/logs/view")
@require_admin
def admin_logs_view():
    return render_template("admin_logs.html")

@app.route("/admin/logs/export")
@require_admin
def export_logs_csv():
    si = io.StringIO()
    writer = csv.writer(si)
    writer.writerow(["IP Address", "Path", "Timestamp"])

    for entry in access_log:
        writer.writerow([entry["ip"], entry["path"], entry["time"]])

    output = make_response(si.getvalue())
    output.headers["Content-Disposition"] = "attachment; filename=access_logs.csv"
    output.headers["Content-type"] = "text/csv"
    return output
    

async def async_download_to_file(session, url, dest_path, request_id, max_size=MAX_UPLOAD_SIZE):
    """Download file asynchronously with size validation."""
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
            cpu = psutil.cpu_percent(interval=None)
            ram = psutil.virtual_memory()
            
            # Use appropriate disk root for platform
            disk_root = 'C:\\' if platform.system() == 'Windows' else '/'
            disk = psutil.disk_usage(disk_root)
            
            boot_time = datetime.datetime.fromtimestamp(psutil.boot_time())
            uptime = str(datetime.datetime.now() - boot_time)

            with generated_server_lock:
                count = generated_server_count

            stats = {
                "cpu": cpu,
                "ram_percent": ram.percent,
                "ram_used": round(ram.used / (1024 ** 3), 2),
                "ram_total": round(ram.total / (1024 ** 3), 2),
                "disk_percent": disk.percent,
                "disk_used": round(disk.used / (1024 ** 3), 2),
                "disk_total": round(disk.total / (1024 ** 3), 2),
                "uptime": uptime,
                "active_users": len(active_users),
                "platform": platform.system(),
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


@app.route("/admin/dashboard")
@require_admin
def admin_dashboard():
    return render_template("admin_stats.html")



def push_log(request_id, message):
    """Thread-safe log pushing with validation."""
    if not request_id or not isinstance(request_id, str):
        logging.warning(f"Invalid request_id for log: {request_id}")
        return
    
    # Sanitize message to prevent log injection
    safe_message = str(message).replace('\n', ' ').replace('\r', '')[:1000]
    log_line = f"{safe_message}"
    
    # Ensure lock exists for this request_id
    if request_id not in log_locks:
        log_locks[request_id] = Lock()
    
    with log_locks[request_id]:
        if request_id not in log_buffers:
            log_buffers[request_id] = []
        log_buffers[request_id].append(log_line)
    
    logging.info(f"[{request_id}] {safe_message}")
    

@app.route("/api/logs/<request_id>")
def stream_logs(request_id):
    """Stream logs for a specific request ID."""
    # Validate request_id
    request_id, error = validate_request_id(request_id)
    if error:
        return jsonify({"error": error}), 400
    
    def generate():
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
                yield f"data: {line}\n\n"
            last_index = len(logs)
        
        # Keep-alive counter
        keepalive_counter = 0
        
        # Then continue streaming new logs
        max_iterations = 36000  # 3 hours max (0.3s * 36000 = 10800s)
        iteration = 0
        while iteration < max_iterations:
            time.sleep(0.3)  # Check more frequently
            keepalive_counter += 1
            iteration += 1
            
            # Send keep-alive every 10 seconds (every ~33 iterations)
            if keepalive_counter % 33 == 0:
                yield f": keepalive\n\n"
            
            new_logs = []
            with log_locks[request_id]:
                logs = log_buffers[request_id] if request_id in log_buffers else []
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
    return Response(generate(), mimetype="text/event-stream", headers={
        'Cache-Control': 'no-cache',
        'X-Accel-Buffering': 'no'
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
    
    # Validate file upload
    if 'mrpack' not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    
    mrpack_file = request.files['mrpack']
    if not mrpack_file.filename:
        return jsonify({"error": "No filename provided"}), 400
    
    # Validate file extension
    if not mrpack_file.filename.lower().endswith('.mrpack'):
        return jsonify({"error": "Invalid file type. Only .mrpack files are allowed."}), 400
    
    # Initialize log buffer and lock for this request if they don't exist
    if request_id not in log_buffers:
        if request_id not in log_locks:
            log_locks[request_id] = Lock()
        with log_locks[request_id]:
            log_buffers[request_id] = []
    
    push_log(request_id, "🛠 Starting server generation...")
    mrpack_file = request.files['mrpack']
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
                installer_file = request.files.get('quilt_installer')
                if not installer_file or not installer_file.filename.startswith('quilt-installer'):
                    push_log(request_id, "❌ Quilt installer not uploaded or named incorrectly (must be 'quilt-installer*.jar').")
                    return jsonify({
                        "error": "missing_quilt_installer",
                        "message": "Please upload a valid quilt-installer.jar.",
                        "popup": "quilt_installer_required",
                        "download_link": "https://quiltmc.org/install"
                    }), 400
                try:
                    setup_quilt(mc_version, loader_version, server_dir, request_id, installer_file)
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
                        setup_fabric(mc_version, loader_version, server_dir, request_id)
                    elif loader_type == 'forge':
                        setup_forge(mc_version, loader_version, server_dir, request_id)
                    elif loader_type == 'neoforge':
                        setup_neoforge(mc_version, loader_version, server_dir, request_id, include_starter_jar=True)
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


def download_to_file(url, dest, request_id, max_size=MAX_UPLOAD_SIZE):
    """Download file with size validation and error handling."""
    try:
        push_log(request_id, f"Downloading: {url}")
        
        # Validate URL
        if not url or not isinstance(url, str):
            raise ValueError("Invalid URL")
        
        with requests.get(url, stream=True, timeout=30, allow_redirects=True) as r:
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
    except Exception as e:
        # Clean up partial file on error
        if os.path.exists(dest):
            try:
                os.remove(dest)
            except OSError:
                pass
        logging.error(f"[{request_id}] Failed to download {url}: {e}")
        raise


def copy_overrides(src, dst, request_id):
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
    installer_path = os.path.join(tempfile.gettempdir(), f"fabric-installer-{request_id}.jar")
    if not os.path.exists(installer_path):
        meta_url = "https://meta.fabricmc.net/v2/versions/installer"
        try:
            installer_meta = requests.get(meta_url, timeout=15).json()[0]
            download_to_file(installer_meta['url'], installer_path, request_id)
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
    java_version = resolve_java_version("fabric", mc_version)
    java_path = get_java_path(java_version)
    push_log(request_id, f"🧩 Using Java {java_version} for Fabric {mc_version}")
    queue = Queue()
    args = ["server", "-downloadMinecraft", "-mcversion", mc_version, "-loader", loader_version, "-dir", server_dir]
    p = Process(target=run_installer, args=(java_path, installer_path, args, server_dir, request_id, queue))
    p.start()
    
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
    
    if stdout:
        push_log(request_id, f"Fabric installer output: {stdout}")
    if returncode != 0:
        error_msg = f"Fabric installer failed with exit code {returncode}. Download the Fabric installer manually from: {meta_url}"
        push_log(request_id, f"❌ {error_msg}")
        raise RuntimeError(json.dumps({
            "error": "Fabric installer failed",
            "message": stdout,
            "download_link": meta_url
        }))
    dir_contents = os.listdir(server_dir)
    push_log(request_id, f"Server directory contents after Fabric install: {dir_contents}")
    jar_files = [f for f in dir_contents if f.endswith('.jar') and 'installer' not in f.lower()]
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
        if os.path.exists(target_server_jar):
            os.remove(target_server_jar)  # Remove old server.jar if it exists
        os.rename(server_jar, target_server_jar)
        push_log(request_id, f"Renamed {target_jar} to server.jar")
    elif not os.path.exists(target_server_jar):
        error_msg = f"server.jar not found. Download the Fabric installer manually from: {meta_url}"
        push_log(request_id, f"❌ {error_msg}")
        raise RuntimeError(json.dumps({
            "error": "server.jar not found",
            "message": "Fabric setup failed to generate server.jar.",
            "download_link": meta_url
        }))
    push_log(request_id, "Fabric server setup complete")
    eula_path = os.path.join(server_dir, "eula.txt")
    with open(eula_path, "w") as f:
        f.write("eula=false\n")
    push_log(request_id, "📝 Created eula.txt with eula=false")
    os.remove(installer_path)
    push_log(request_id, f"Deleted Fabric installer: {installer_path}")


def setup_forge(mc_version, loader_version, server_dir, request_id):
    version = f"{mc_version}-{loader_version}"
    url = f"https://maven.minecraftforge.net/net/minecraftforge/forge/{version}/forge-{version}-installer.jar"
    installer_path = os.path.join(tempfile.gettempdir(), f"forge-installer-{version}-{request_id}.jar")
    try:
        download_to_file(url, installer_path, request_id)
    except Exception as e:
        error_msg = f"Failed to download Forge installer: {str(e)}. Download manually from: https://files.minecraftforge.net/"
        push_log(request_id, f"❌ {error_msg}")
        raise RuntimeError(json.dumps({
            "error": "Installer download failed",
            "message": str(e),
            "download_link": "https://files.minecraftforge.net/"
        }))
    if os.path.getsize(installer_path) < MIN_INSTALLER_SIZE:
        error_msg = f"Forge installer is too small or corrupt. Download manually from: {url}"
        push_log(request_id, f"❌ {error_msg}")
        raise RuntimeError(json.dumps({
            "error": "Invalid installer",
            "message": "Downloaded Forge installer is corrupt or empty.",
            "download_link": url
        }))
    with zipfile.ZipFile(installer_path) as zf:
        if zf.testzip() is not None:
            error_msg = f"Forge installer is corrupt. Download manually from: {url}"
            push_log(request_id, f"❌ {error_msg}")
            raise RuntimeError(json.dumps({
                "error": "Corrupt installer",
                "message": "Downloaded Forge installer is corrupt.",
                "download_link": url
            }))
    if not os.access(server_dir, os.W_OK):
        push_log(request_id, f"No write permissions for {server_dir}")
        raise RuntimeError(json.dumps({
            "error": "Permission denied",
            "message": f"Cannot write to server directory: {server_dir}",
            "download_link": url
        }))
    disk = psutil.disk_usage(server_dir)
    if disk.free < MIN_DISK_SPACE:
        error_msg = f"Insufficient disk space ({disk.free / 1024**3:.2f}GB free). Download manually from: {url}"
        push_log(request_id, f"❌ {error_msg}")
        raise RuntimeError(json.dumps({
            "error": "Insufficient disk space",
            "message": f"Not enough disk space to run installer.",
            "download_link": url
        }))
    mem = psutil.virtual_memory()
    if mem.available < MIN_MEMORY:
        error_msg = f"Insufficient memory ({mem.available / 1024**2:.2f}MB free). Download manually from: {url}"
        push_log(request_id, f"❌ {error_msg}")
        raise RuntimeError(json.dumps({
            "error": "Insufficient memory",
            "message": f"Not enough memory to run installer.",
            "download_link": url
        }))
    java_version = resolve_java_version("forge", mc_version)
    java_path = get_java_path(java_version)
    if not os.path.exists(java_path):
        error_msg = f"Java {java_version} not found at {java_path}. Download manually from: {url}"
        push_log(request_id, f"❌ {error_msg}")
        raise RuntimeError(json.dumps({
            "error": "Java not found",
            "message": f"Java {java_version} not found at {java_path}",
            "download_link": url
        }))
    push_log(request_id, f"🧩 Using Java {java_version} for Forge {mc_version} at {java_path}")
    queue = Queue()
    args = ["--installServer"]
    p = Process(target=run_installer, args=(java_path, installer_path, args, server_dir, request_id, queue))
    p.start()
    p.join(timeout=360)
    if p.is_alive():
        error_msg = f"Forge installer process timed out. Download manually from: {url}"
        push_log(request_id, f"❌ {error_msg}")
        raise RuntimeError(json.dumps({
            "error": "Installer timeout",
            "message": "Forge installer process took too long.",
            "download_link": url
        }))
    # Get result from queue with timeout to prevent blocking
    try:
        returncode, stdout = queue.get(timeout=5)
    except Exception:
        returncode, stdout = -1, "Failed to get installer output from queue"
    push_log(request_id, f"Forge installer output: {stdout}")
    if returncode != 0:
        error_msg = f"Forge installer failed with exit code {returncode}. Download manually from: {url}"
        push_log(request_id, f"❌ {error_msg}")
        raise RuntimeError(json.dumps({
            "error": "Forge installer failed",
            "message": stdout,
            "download_link": url
        }))
    dir_contents = os.listdir(server_dir)
    push_log(request_id, f"Server directory contents after Forge install: {dir_contents}")
    jar_files = [f for f in dir_contents if f.endswith('.jar') and 'installer' not in f.lower()]
    if not jar_files:
        push_log(request_id, "Failed to find Forge server JAR files.")
        jar_files = [f for f in os.listdir(server_dir) if f.endswith('.jar') and 'installer' not in f.lower()]
        if not jar_files:
            error_msg = f"No JAR files found. Download manually from: {url}"
            push_log(request_id, f"❌ {error_msg}")
            raise RuntimeError(json.dumps({
                "error": "No JAR files found",
                "message": "Forge setup failed to generate a server JAR.",
                "download_link": url
            }))
    if len(jar_files) < 1:
        error_msg = f"Expected at least one JAR file, but found {len(jar_files)}. Download manually from: {url}"
        push_log(request_id, f"❌ {error_msg}")
        raise RuntimeError(json.dumps({
            "error": "No valid JAR files found",
            "message": "Forge setup failed to generate a valid server JAR.",
            "download_link": url
        }))
    push_log(request_id, f"Found {len(jar_files)} JAR files: {', '.join(jar_files)}")
    
    # Prioritize server JAR files - look for actual server JAR, not launcher or installer
    server_jar_candidates = []
    for jar_file in jar_files:
        jar_lower = jar_file.lower()
        # Skip launcher and installer JARs
        if 'launch' in jar_lower or 'installer' in jar_lower or 'client' in jar_lower:
            continue
        # Prioritize files with "server" in name, or forge server files
        if 'server' in jar_lower or 'forge' in jar_lower:
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
        error_msg = f"Forge setup failed: No valid server JAR found. Download manually from: {url}"
        push_log(request_id, f"❌ {error_msg}")
        raise RuntimeError(json.dumps({
            "error": "server.jar not found",
            "message": "Forge setup failed: No valid server JAR found.",
            "download_link": url
        }))
    
    if len(jar_files) > 1:
        push_log(request_id, f"Multiple JAR files found: {', '.join(jar_files)}. Using {target_jar}.")
    
    server_jar = os.path.join(server_dir, target_jar)
    target_server_jar = os.path.join(server_dir, "server.jar")
    
    if os.path.exists(server_jar):
        if os.path.exists(target_server_jar):
            os.remove(target_server_jar)  # Remove old server.jar if it exists
        os.rename(server_jar, target_server_jar)
        push_log(request_id, f"Renamed {target_jar} to server.jar")
    else:
        error_msg = f"Forge setup failed: {target_jar} not found. Download manually from: {url}"
        push_log(request_id, f"❌ {error_msg}")
        raise RuntimeError(json.dumps({
            "error": "server.jar not found",
            "message": f"Forge setup failed: {target_jar} not found.",
            "download_link": url
        }))
    push_log(request_id, "Forge server setup complete")
    eula_path = os.path.join(server_dir, "eula.txt")
    with open(eula_path, "w") as f:
        f.write("eula=false\n")
    push_log(request_id, "📝 Created eula.txt with eula=false")
    os.remove(installer_path)
    push_log(request_id, f"Deleted Forge installer: {installer_path}")


def setup_neoforge(mc_version, loader_version, server_dir, request_id, include_starter_jar=True):
    """
    Setup NeoForge server in the given server_dir.
    Note: server_dir already contains mods/, config/, and other override files from the modpack.
    This function only installs the NeoForge server JAR and creates server.jar.
    """
    api_url = "https://maven.neoforged.net/api/maven/versions/releases/net/neoforged/neoforge"
    push_log(request_id, "Fetching NeoForge version list from Maven")
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
    push_log(request_id, f"Resolved NeoForge installer for {latest}")
    installer_path = os.path.join(tempfile.gettempdir(), f"neoforge-installer-{latest}-{request_id}.jar")
    try:
        download_to_file(installer_url, installer_path, request_id)
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
    push_log(request_id, f"🧩 Using Java {java_version} for NeoForge {mc_version}")
    queue = Queue()
    args = ["--server.jar", "--installServer"]
    p = Process(target=run_installer, args=(java_path, installer_path, args, server_dir, request_id, queue))
    p.start()
    
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
    
    if stdout:
        push_log(request_id, f"NeoForge installer output: {stdout}")
    if returncode != 0:
        error_msg = f"NeoForge installer failed with exit code {returncode}. Download manually from: {installer_url}"
        push_log(request_id, f"❌ {error_msg}")
        raise RuntimeError(json.dumps({
            "error": "NeoForge installer failed",
            "message": stdout,
            "download_link": installer_url
        }))
    dir_contents = os.listdir(server_dir)
    push_log(request_id, f"Server directory contents after NeoForge install: {dir_contents}")
    jar_files = [f for f in dir_contents if f.endswith('.jar') and 'installer' not in f.lower()]
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
        if os.path.exists(target_server_jar):
            os.remove(target_server_jar)  # Remove old server.jar if it exists
        try:
            # Try rename first (faster if same filesystem)
            os.rename(server_jar, target_server_jar)
            push_log(request_id, f"Renamed {target_jar} to server.jar")
        except OSError:
            # If rename fails (different filesystem), use copy
            shutil.copy2(server_jar, target_server_jar)
            push_log(request_id, f"Copied {target_jar} to server.jar")
    elif not os.path.exists(target_server_jar):
        error_msg = f"NeoForge setup failed: server.jar not found. Download manually from: {installer_url}"
        push_log(request_id, f"❌ {error_msg}")
        raise RuntimeError(json.dumps({
            "error": "server.jar not found",
            "message": f"NeoForge setup failed: server.jar was not created.",
            "download_link": installer_url
        }))
    push_log(request_id, "NeoForge server setup complete")
    eula_path = os.path.join(server_dir, "eula.txt")
    with open(eula_path, "w") as f:
        f.write("eula=false\n")
    push_log(request_id, "📝 Created eula.txt with eula=false")
    os.remove(installer_path)
    push_log(request_id, f"Deleted NeoForge installer: {installer_path}")


def setup_quilt(mc_version, loader_version, server_dir, request_id, installer_file):
    installer_path = os.path.join(tempfile.gettempdir(), f"quilt-installer-{request_id}.jar")
    installer_file.save(installer_path)
    push_log(request_id, f"Received Quilt installer: {installer_file.filename}")
    
    if not os.access(server_dir, os.W_OK):
        push_log(request_id, f"No write permissions for {server_dir}")
        raise RuntimeError(json.dumps({
            "error": "Permission denied",
            "message": f"Cannot write to server directory: {server_dir}",
            "download_link": "https://quiltmc.org/install"
        }))
    
    java_version = resolve_java_version("quilt", mc_version)
    java_path = get_java_path(java_version)
    if not os.path.exists(java_path):
        error_msg = f"Java {java_version} not found at {java_path}. Download the Quilt installer manually from: https://quiltmc.org/install"
        push_log(request_id, f"❌ {error_msg}")
        raise RuntimeError(json.dumps({
            "error": "Java not found",
            "message": f"Java {java_version} not found at {java_path}",
            "download_link": "https://quiltmc.org/install"
        }))
    
    push_log(request_id, f"🧩 Using Java {java_version} for Quilt {mc_version} at {java_path}")
    
    # Validate installer
    if os.path.getsize(installer_path) < MIN_INSTALLER_SIZE:
        error_msg = f"Quilt installer is too small or corrupt. Download manually from: https://quiltmc.org/install"
        push_log(request_id, f"❌ {error_msg}")
        raise RuntimeError(json.dumps({
            "error": "Invalid installer",
            "message": "Downloaded Quilt installer is corrupt or empty.",
            "download_link": "https://quiltmc.org/install"
        }))
    
    with zipfile.ZipFile(installer_path) as zf:
        if zf.testzip() is not None:
            error_msg = f"Quilt installer is corrupt. Download manually from: https://quiltmc.org/install"
            push_log(request_id, f"❌ {error_msg}")
            raise RuntimeError(json.dumps({
                "error": "Corrupt installer",
                "message": "Downloaded Quilt installer is corrupt.",
                "download_link": "https://quiltmc.org/install"
            }))
    
    queue = Queue()
    
    # Try different argument formats to see which one works
    # Option 1: Using equals sign (as shown in help)
    args = ["install", "server", mc_version, loader_version, f"--install-dir={server_dir}", "--download-server"]
    
    # Log the exact command we're running for debugging
    push_log(request_id, f"Running command: {java_path} -jar {installer_path} {' '.join(args)}")
    
    p = Process(target=run_installer, args=(java_path, installer_path, args, server_dir, request_id, queue))
    p.start()
    p.join(timeout=360)
    
    if p.is_alive():
        p.terminate()
        error_msg = f"Quilt installer process timed out. Download the Quilt installer manually from: https://quiltmc.org/install"
        push_log(request_id, f"❌ {error_msg}")
        raise RuntimeError(json.dumps({
            "error": "Installer timeout",
            "message": "Quilt installer process took too long.",
            "download_link": "https://quiltmc.org/install"
        }))
    
    # Get result from queue with timeout to prevent blocking
    try:
        returncode, stdout = queue.get(timeout=5)
    except Exception:
        returncode, stdout = -1, "Failed to get installer output from queue"
    push_log(request_id, f"Quilt installer output: {stdout}")
    
    if returncode != 0:
        # If the first attempt fails, try the alternative format
        push_log(request_id, "First attempt failed, trying alternative argument format")
        args = ["install", "server", mc_version, loader_version, "--install-dir", server_dir, "--download-server"]
        push_log(request_id, f"Running alternative command: {java_path} -jar {installer_path} {' '.join(args)}")
        
        p = Process(target=run_installer, args=(java_path, installer_path, args, server_dir, request_id, queue))
        p.start()
        p.join(timeout=360)
        
        if p.is_alive():
            p.terminate()
            error_msg = f"Quilt installer process timed out. Download the Quilt installer manually from: https://quiltmc.org/install"
            push_log(request_id, f"❌ {error_msg}")
            raise RuntimeError(json.dumps({
                "error": "Installer timeout",
                "message": "Quilt installer process took too long.",
                "download_link": "https://quiltmc.org/install"
            }))
        
        # Get result from queue with timeout to prevent blocking
        try:
            returncode, stdout = queue.get(timeout=5)
        except Exception:
            returncode, stdout = -1, "Failed to get installer output from queue"
        push_log(request_id, f"Quilt installer (2nd attempt) output: {stdout}")
        
        if returncode != 0:
            error_msg = f"Quilt installer failed with exit code {returncode}. Download the Quilt installer manually from: https://quiltmc.org/install"
            push_log(request_id, f"❌ {error_msg}")
            raise RuntimeError(json.dumps({
                "error": "Quilt installer failed",
                "message": stdout,
                "download_link": "https://quiltmc.org/install"
            }))
    
    dir_contents = os.listdir(server_dir)
    push_log(request_id, f"Server directory contents after Quilt install: {dir_contents}")
    
    # Look for any JAR file in the directory
    jar_files = [f for f in dir_contents if f.endswith('.jar') and 'installer' not in f.lower()]
    
    if not jar_files:
        push_log(request_id, "Failed to find Quilt server JAR files.")
        error_msg = f"No JAR files found. Download the Quilt installer manually from: https://quiltmc.org/install"
        push_log(request_id, f"❌ {error_msg}")
        raise RuntimeError(json.dumps({
            "error": "No JAR files found",
            "message": "Quilt setup failed to generate a server JAR.",
            "download_link": "https://quiltmc.org/install"
        }))
    
    push_log(request_id, f"Found {len(jar_files)} JAR files: {', '.join(jar_files)}")
    
    # Prioritize server JAR files - look for actual server JAR, not launcher or installer
    server_jar_candidates = []
    for jar_file in jar_files:
        jar_lower = jar_file.lower()
        # Skip launcher and installer JARs
        if 'launch' in jar_lower or 'installer' in jar_lower or 'client' in jar_lower:
            continue
        # Prioritize files with "server" in name, or quilt server files
        if 'server' in jar_lower or 'quilt' in jar_lower:
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
        error_msg = f"Quilt setup failed: No valid server JAR found. Download the Quilt installer manually from: https://quiltmc.org/install"
        push_log(request_id, f"❌ {error_msg}")
        raise RuntimeError(json.dumps({
            "error": "No JAR files found",
            "message": "Quilt setup failed to generate a server JAR.",
            "download_link": "https://quiltmc.org/install"
        }))
    
    if len(jar_files) > 1:
        push_log(request_id, f"Multiple JAR files found: {', '.join(jar_files)}. Using {target_jar}.")
    
    server_jar = os.path.join(server_dir, target_jar)
    target_server_jar = os.path.join(server_dir, "server.jar")
    
    if os.path.exists(server_jar) and target_jar != "server.jar":
        if os.path.exists(target_server_jar):
            os.remove(target_server_jar)  # Remove old server.jar if it exists
        os.rename(server_jar, target_server_jar)
        push_log(request_id, f"Renamed {target_jar} to server.jar")
    elif not os.path.exists(target_server_jar):
        # If there's no server.jar, copy the target JAR file
        shutil.copy(server_jar, target_server_jar)
        push_log(request_id, f"Copied {target_jar} to server.jar")
    
    push_log(request_id, "Quilt server setup complete")
    
    eula_path = os.path.join(server_dir, "eula.txt")
    with open(eula_path, "w") as f:
        f.write("eula=false\n")
    push_log(request_id, "📝 Created eula.txt with eula=false")
    
    os.remove(installer_path)
    push_log(request_id, f"Deleted Quilt installer: {installer_path}")


if __name__ == '__main__':
    try:
        subprocess.run(["java", "-version"], check=True)
    except Exception as e:
        logging.warning("Default Java not available or not installed.")
    initialize_server_count()
    socketio.run(app, debug=False, port=8090)