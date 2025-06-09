from flask import Flask, request, send_file, render_template, jsonify , Response
import zipfile, requests, shutil, json, os, tempfile, subprocess, logging, uuid
from io import BytesIO
from collections import defaultdict
import threading
import time
import re

app = Flask(__name__)

log_buffers = defaultdict(list)
log_locks = defaultdict(threading.Lock)

def push_log(request_id, message):
    log_line = f"{message}"
    with log_locks[request_id]:
        log_buffers[request_id].append(log_line)
    logging.info(f"[{request_id}] {message}")

@app.route("/api/logs/<request_id>")
def stream_logs(request_id):
    def generate():
        last_index = 0
        while True:
            time.sleep(0.5)
            with log_locks[request_id]:
                logs = log_buffers[request_id]
                new_logs = logs[last_index:]
                last_index = len(logs)
            for line in new_logs:
                yield f"data: {line}\n\n"
    return Response(generate(), mimetype="text/event-stream")

@app.route("/", methods=["GET"])
def home():
    logging.info("GET / - Rendering index.html")
    return render_template("index.html")

@app.route("/api/generate", methods=["POST"])
def generate_server():
    request_id = request.args.get("request_id")
    if not request_id:
        return jsonify({"error": "Missing request ID"}), 400

    push_log(request_id, "Processing server generation request")

    if 'mrpack' not in request.files:
        logging.warning(f"[{request_id}] No .mrpack file uploaded")
        return jsonify({"error": "No file uploaded"}), 400

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
            safe_name = re.sub(r'[^a-zA-Z0-9_-]', '_', base_name)  # Sanitize filename
            server_dir = os.path.join(tmp_dir, f"{safe_name}-MSFG")
            os.makedirs(os.path.join(server_dir, "mods"), exist_ok=True)


            # Download mods
            for mod in index_data["files"]:
                url = mod['downloads'][0]
                filename = mod.get('fileName')
                if not filename:
                    filename = os.path.basename(url)
                download_to_file(url, os.path.join(server_dir, "mods", filename), request_id)

            # Copy overrides
            overrides_dir = os.path.join(extract_path, "overrides")
            if os.path.exists(overrides_dir):
                copy_overrides(overrides_dir, server_dir, request_id)

            # Install server
            if loader_type == 'quilt':
                installer_file = request.files.get('quilt_installer')

                if not installer_file:
                    for file in request.files.values():
                        if file.filename.endswith('.jar'):
                            installer_file = file
                            break

                if not installer_file:
                    push_log(request_id, "❌ Quilt installer not uploaded or named incorrectly.")
                    return jsonify({
                        "error": "missing_quilt_installer",
                        "message": "This modpack uses Quilt. Please upload quilt-installer.jar to continue.",
                        "popup": "quilt_installer_required"
                    }), 400

                try:
                    setup_quilt(mc_version, loader_version, server_dir, request_id, installer_file)
                except Exception as e:
                    push_log(request_id, f"❌ Server setup error: {e}")
                    logging.error(f"[{request_id}] Server setup error: {e}")
                    return jsonify({"error": str(e)}), 500

            else:
                try:
                    if loader_type == 'fabric':
                        setup_fabric(mc_version, loader_version, server_dir, request_id)
                    elif loader_type == 'forge':
                        setup_forge(mc_version, loader_version, server_dir, request_id)
                    elif loader_type == 'neoforge':
                        setup_neoforge(mc_version, loader_version, server_dir, request_id)
                    else:
                        setup_vanilla(mc_version, server_dir, request_id)
                except Exception as e:
                    push_log(request_id, f"❌ Server setup error: {e}")
                    logging.error(f"[{request_id}] Server setup error: {e}")
                    return jsonify({"error": str(e)}), 500

            # Zip output
            zip_buffer = BytesIO()
            with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_out:
                for root, _, files in os.walk(server_dir):
                    for file in files:
                        abs_file = os.path.join(root, file)
                        arcname = os.path.join(f"{safe_name}-MSFG", os.path.relpath(abs_file, server_dir))
                        zip_out.write(abs_file, arcname)
            zip_buffer.seek(0)

            push_log(request_id, "Server zip created successfully")
            base_name = os.path.splitext(mrpack_file.filename)[0]
            zip_name = f"{safe_name}-MSFG.zip"
            push_log(request_id, f"Preparing download: {zip_name}")
            return send_file(zip_buffer, as_attachment=True, download_name=zip_name, mimetype="application/zip")


        except Exception as e:
            push_log(request_id, f"❌ Unexpected error: {e}")
            logging.exception(f"[{request_id}] Unexpected error: {e}")
            return jsonify({"error": "Internal server error"}), 500


def detect_loader(deps):
    for loader in ['fabric-loader', 'forge', 'quilt-loader', 'neoforge']:
        if loader in deps:
            return loader.split('-')[0], deps[loader]
    return 'vanilla', None


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


def download_to_file(url, dest, request_id):
    try:
        push_log(request_id, f"Downloading: {url}")
        with requests.get(url, stream=True, timeout=30) as r:
            r.raise_for_status()

            with open(dest, 'wb') as f:
                shutil.copyfileobj(r.raw, f)
        push_log(request_id, f"Downloaded to {dest}")
    except Exception as e:
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


def setup_fabric(mc_version, loader_version, server_dir, request_id):
    installer_path = "/tmp/fabric-installer.jar"
    if not os.path.exists(installer_path):
        meta_url = "https://meta.fabricmc.net/v2/versions/installer"
        installer_meta = requests.get(meta_url, timeout=15).json()[0]
        download_to_file(installer_meta['url'], installer_path, request_id)

    subprocess.run([
        "java", "-jar", installer_path,
        "server", "-downloadMinecraft",
        "-mcversion", mc_version,
        "-loader", loader_version,
        "-dir", server_dir
    ], check=True)
    push_log(request_id, "Fabric server setup complete")
    os.remove(installer_path)
    push_log(request_id, f"Deleted Fabric installer: {installer_path}")


def setup_forge(mc_version, loader_version, server_dir, request_id):
    version = f"{mc_version}-{loader_version}"
    url = f"https://maven.minecraftforge.net/net/minecraftforge/forge/{version}/forge-{version}-installer.jar"
    installer_path = os.path.join("/tmp", f"forge-{version}.jar")
    if not os.path.exists(installer_path):
        download_to_file(url, installer_path, request_id)

    subprocess.run(["java", "-jar", installer_path, "--installServer"], cwd=server_dir, check=True)
    push_log(request_id, "Forge server setup complete")
    os.remove(installer_path)
    push_log(request_id, f"Deleted Forge installer: {installer_path}")


def setup_neoforge(mc_version, loader_version, server_dir, request_id):
    version_string = f"{mc_version}-{loader_version}"
    api_url = f"https://maven.neoforged.net/api/maven/versions/releases/net/neoforged/neoforge"
    push_log(request_id, f"Fetching NeoForge version list from Maven")

    resp = requests.get(api_url, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    # Filter versions matching loader version
    matches = [v for v in data["versions"] if v.endswith(loader_version)]
    if not matches:
        raise Exception(f"No NeoForge version matching loader '{loader_version}' found")

    # Pick the latest match
    latest = sorted(matches, key=lambda v: tuple(map(int, re.findall(r"\d+", v))))[-1]
    installer_url = f"https://maven.neoforged.net/releases/net/neoforged/neoforge/{latest}/neoforge-{latest}-installer.jar"

    push_log(request_id, f"Resolved NeoForge installer for {latest}")
    installer_path = os.path.join(tempfile.gettempdir(), f"neoforge-{latest}.jar")
    download_to_file(installer_url, installer_path, request_id)

    push_log(request_id, f"Running NeoForge installer...")
    result = subprocess.run(
        ["java", "-jar", installer_path, "--installServer"],
        cwd=server_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True
    )
    push_log(request_id, result.stdout)
    result.check_returncode()

    push_log(request_id, "NeoForge server setup complete")
    os.remove(installer_path)
    push_log(request_id, f"Deleted NeoForge installer: {installer_path}")


def setup_quilt(mc_version, loader_version, server_dir, request_id, installer_file):
    installer_path = os.path.join(tempfile.gettempdir(), "quilt-installer.jar")
    installer_file.save(installer_path)
    push_log(request_id, f"Received Quilt installer: {installer_file.filename}")

    result = subprocess.run([
        "java", "-jar", installer_path,
        "install", "server",
        "-minecraft", mc_version,
        "-loader", loader_version,
        "--install-dir", server_dir
    ], cwd=server_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    push_log(request_id, result.stdout)
    result.check_returncode()
    os.remove(installer_path)
    push_log(request_id, f"Deleted Quilt installer: {installer_path}")


def setup_vanilla(mc_version, server_dir, request_id):
    manifest_url = "https://launchermeta.mojang.com/mc/game/version_manifest.json"
    manifest = requests.get(manifest_url, timeout=10).json()
    version_info = next((v for v in manifest["versions"] if v["id"] == mc_version), None)
    if not version_info:
        raise Exception("MC version not found")
    version_data = requests.get(version_info["url"], timeout=10).json()
    jar_url = version_data["downloads"]["server"]["url"]
    download_to_file(jar_url, os.path.join(server_dir, "server.jar"), request_id)
    push_log(request_id, "Minecraft server setup completed")


if __name__ == '__main__':
    logging.info("Running in development mode. Use Gunicorn or uWSGI for production.")
    app.run(debug=False, port=8080)
