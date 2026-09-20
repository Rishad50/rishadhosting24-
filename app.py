import os
import sys
import json
import uuid
import shutil
import zipfile
import threading
import subprocess
from datetime import datetime
from flask import Flask, render_template, request, jsonify, send_file
from werkzeug.utils import secure_filename

# -------------------------------------------------------------------------
# APPLICATION SETUP & CONFIGURATION
# -------------------------------------------------------------------------
app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100 MB maximum upload limit

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SERVERS_FILE = os.path.join(BASE_DIR, "servers.json")
BOTS_DIR = os.path.join(BASE_DIR, "bots")

os.makedirs(BOTS_DIR, exist_ok=True)

# Lock for persistent JSON file access and process coordination
DATA_LOCK = threading.Lock()

# In-memory registry for live processes: { server_id: {"process": Popen, "log_file": file_handle} }
RUNNING_PROCESSES = {}

# -------------------------------------------------------------------------
# STORAGE HELPER FUNCTIONS
# -------------------------------------------------------------------------
def load_servers():
    """Load servers registry from servers.json."""
    with DATA_LOCK:
        if not os.path.exists(SERVERS_FILE):
            with open(SERVERS_FILE, "w", encoding="utf-8") as f:
                json.dump({}, f, indent=4)
            return {}
        try:
            with open(SERVERS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

def save_servers(servers):
    """Save servers registry to servers.json."""
    with DATA_LOCK:
        with open(SERVERS_FILE, "w", encoding="utf-8") as f:
            json.dump(servers, f, indent=4)

def get_server_dir(server_id):
    """Retrieve absolute directory path for a server."""
    return os.path.join(BOTS_DIR, server_id)

def is_safe_path(base_dir, target_path):
    """Prevent path traversal attacks (e.g. ../, symlinks)."""
    base_dir = os.path.abspath(base_dir)
    target_path = os.path.abspath(target_path)
    return target_path.startswith(base_dir)

def sync_server_process_statuses():
    """Poll processes and update server status if terminated."""
    servers = load_servers()
    modified = False

    for server_id, server in servers.items():
        proc_info = RUNNING_PROCESSES.get(server_id)
        if proc_info:
            poll = proc_info["process"].poll()
            if poll is not None:
                # Process exited naturally or crashed
                server["status"] = "stopped"
                server["pid"] = None
                try:
                    if proc_info["log_file"] and not proc_info["log_file"].closed:
                        proc_info["log_file"].close()
                except Exception:
                    pass
                RUNNING_PROCESSES.pop(server_id, None)
                modified = True
        else:
            if server.get("status") == "running":
                server["status"] = "stopped"
                server["pid"] = None
                modified = True

    if modified:
        save_servers(servers)
    return servers

# -------------------------------------------------------------------------
# HOME ROUTE
# -------------------------------------------------------------------------
@app.route("/")
def home():
    servers = sync_server_process_statuses()
    return render_template("home.html", servers=servers)

# -------------------------------------------------------------------------
# CORE SERVER MANAGEMENT APIS
# -------------------------------------------------------------------------
@app.route("/api/servers", methods=["GET"])
def get_all_servers():
    servers = sync_server_process_statuses()
    return jsonify({"status": "success", "servers": servers})

@app.route("/api/server/<server_id>", methods=["GET"])
def get_server(server_id):
    servers = sync_server_process_statuses()
    server = servers.get(server_id)
    if not server:
        return jsonify({"status": "error", "message": "Server not found."}), 404
    return jsonify({"status": "success", "server": server})

@app.route("/api/create_server", methods=["POST"])
def create_server():
    data = request.get_json() or {}
    name = data.get("name", "").strip()
    server_type = data.get("type", "Python").strip()
    ram = data.get("ram", "1 GB").strip()
    disk = data.get("disk", "1 GB").strip()

    if not name:
        return jsonify({"status": "error", "message": "Server name cannot be empty."}), 400

    server_id = uuid.uuid4().hex[:12]
    server_dir = get_server_dir(server_id)
    os.makedirs(server_dir, exist_ok=True)

    # Initial boilerplate files
    main_py_path = os.path.join(server_dir, "main.py")
    req_txt_path = os.path.join(server_dir, "requirements.txt")
    log_path = os.path.join(server_dir, "output.log")

    sample_main = (
        'import time\n'
        'import sys\n\n'
        'print("Server started successfully!", flush=True)\n'
        'counter = 1\n'
        'while True:\n'
        '    print(f"Heartbeat #{counter} | Active", flush=True)\n'
        '    counter += 1\n'
        '    time.sleep(5)\n'
    )

    with open(main_py_path, "w", encoding="utf-8") as f:
        f.write(sample_main)

    with open(req_txt_path, "w", encoding="utf-8") as f:
        f.write("# Enter Python package dependencies here\n")

    with open(log_path, "w", encoding="utf-8") as f:
        f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Server initialized.\n")

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    server_data = {
        "id": server_id,
        "name": name,
        "type": server_type,
        "ram": ram,
        "disk": disk,
        "status": "stopped",
        "pid": None,
        "startup_file": "main.py",
        "requirements_file": "requirements.txt",
        "created_at": now,
        "started_at": None
    }

    servers = load_servers()
    servers[server_id] = server_data
    save_servers(servers)

    return jsonify({"status": "success", "message": "Server created successfully.", "server": server_data})

@app.route("/api/start/<server_id>", methods=["POST"])
def start_server(server_id):
    servers = sync_server_process_statuses()
    server = servers.get(server_id)
    if not server:
        return jsonify({"status": "error", "message": "Server not found."}), 404

    if server.get("status") == "running":
        return jsonify({"status": "error", "message": "Server is already running."}), 400

    server_dir = get_server_dir(server_id)
    startup_file = server.get("startup_file", "main.py")
    startup_path = os.path.join(server_dir, startup_file)

    if not os.path.exists(startup_path):
        return jsonify({"status": "error", "message": f"Startup file '{startup_file}' does not exist."}), 400

    log_path = os.path.join(server_dir, "output.log")
    log_file = open(log_path, "a", encoding="utf-8", buffering=1)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_file.write(f"\n[{timestamp}] --- Starting Server ({startup_file}) ---\n")
    log_file.flush()

    try:
        proc = subprocess.Popen(
            [sys.executable, startup_file],
            cwd=server_dir,
            stdin=subprocess.PIPE,
            stdout=log_file,
            stderr=log_file,
            text=True,
            bufsize=1
        )
        RUNNING_PROCESSES[server_id] = {
            "process": proc,
            "log_file": log_file
        }
        server["status"] = "running"
        server["pid"] = proc.pid
        server["started_at"] = timestamp
        save_servers(servers)

        return jsonify({"status": "success", "message": "Server started successfully.", "pid": proc.pid})
    except Exception as e:
        log_file.close()
        return jsonify({"status": "error", "message": f"Failed to start server: {str(e)}"}), 500

@app.route("/api/stop/<server_id>", methods=["POST"])
def stop_server(server_id):
    servers = sync_server_process_statuses()
    server = servers.get(server_id)
    if not server:
        return jsonify({"status": "error", "message": "Server not found."}), 404

    proc_info = RUNNING_PROCESSES.get(server_id)
    if proc_info:
        proc = proc_info["process"]
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except (subprocess.TimeoutExpired, Exception):
            try:
                proc.kill()
            except Exception:
                pass
        try:
            if proc_info["log_file"] and not proc_info["log_file"].closed:
                proc_info["log_file"].write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] --- Server Stopped ---\n")
                proc_info["log_file"].close()
        except Exception:
            pass
        RUNNING_PROCESSES.pop(server_id, None)

    server["status"] = "stopped"
    server["pid"] = None
    save_servers(servers)
    return jsonify({"status": "success", "message": "Server stopped successfully."})

@app.route("/api/restart/<server_id>", methods=["POST"])
def restart_server(server_id):
    stop_resp = stop_server(server_id)
    stop_data = stop_resp.get_json() if hasattr(stop_resp, 'get_json') else {}
    if stop_resp.status_code != 200 and stop_data.get("message") != "Server is not running.":
        return stop_resp

    return start_server(server_id)

@app.route("/api/server/<server_id>", methods=["DELETE"])
def delete_server(server_id):
    stop_server(server_id)
    servers = load_servers()
    if server_id not in servers:
        return jsonify({"status": "error", "message": "Server not found."}), 404

    servers.pop(server_id, None)
    save_servers(servers)

    server_dir = get_server_dir(server_id)
    if os.path.exists(server_dir):
        shutil.rmtree(server_dir, ignore_errors=True)

    return jsonify({"status": "success", "message": "Server deleted successfully."})

# -------------------------------------------------------------------------
# FILE MANAGER APIS
# -------------------------------------------------------------------------
@app.route("/api/files/<server_id>", methods=["GET"])
def list_files(server_id):
    servers = load_servers()
    if server_id not in servers:
        return jsonify({"status": "error", "message": "Server not found."}), 404

    server_dir = get_server_dir(server_id)
    sub_path = request.args.get("path", "").strip().lstrip("/\\")
    target_dir = os.path.normpath(os.path.join(server_dir, sub_path))

    if not is_safe_path(server_dir, target_dir) or not os.path.exists(target_dir):
        return jsonify({"status": "error", "message": "Invalid directory path."}), 400

    items = []
    try:
        with os.scandir(target_dir) as entries:
            for entry in entries:
                stat = entry.stat()
                items.append({
                    "name": entry.name,
                    "is_dir": entry.is_dir(),
                    "size": stat.st_size if entry.is_file() else 0,
                    "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
                })
        items.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
        return jsonify({"status": "success", "path": sub_path, "items": items})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/file/<server_id>", methods=["GET", "POST", "DELETE"])
def handle_file(server_id):
    servers = load_servers()
    if server_id not in servers:
        return jsonify({"status": "error", "message": "Server not found."}), 404

    server_dir = get_server_dir(server_id)

    if request.method == "GET":
        relative_path = request.args.get("path", "").strip().lstrip("/\\")
        target_path = os.path.normpath(os.path.join(server_dir, relative_path))
        if not is_safe_path(server_dir, target_path) or not os.path.isfile(target_path):
            return jsonify({"status": "error", "message": "File not found or invalid."}), 404
        try:
            with open(target_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            return jsonify({"status": "success", "content": content, "path": relative_path})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

    elif request.method == "POST":
        data = request.get_json() or {}
        relative_path = data.get("path", "").strip().lstrip("/\\")
        content = data.get("content", "")
        if not relative_path:
            return jsonify({"status": "error", "message": "Missing file path."}), 400

        target_path = os.path.normpath(os.path.join(server_dir, relative_path))
        if not is_safe_path(server_dir, target_path):
            return jsonify({"status": "error", "message": "Forbidden path."}), 403

        os.makedirs(os.path.dirname(target_path), exist_ok=True)
        try:
            with open(target_path, "w", encoding="utf-8") as f:
                f.write(content)
            return jsonify({"status": "success", "message": "File saved successfully."})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

    elif request.method == "DELETE":
        data = request.get_json() or {}
        relative_path = (data.get("path") or request.args.get("path", "")).strip().lstrip("/\\")
        if not relative_path:
            return jsonify({"status": "error", "message": "Target path required."}), 400

        target_path = os.path.normpath(os.path.join(server_dir, relative_path))
        if not is_safe_path(server_dir, target_path) or not os.path.exists(target_path):
            return jsonify({"status": "error", "message": "Invalid item path."}), 400

        try:
            if os.path.isdir(target_path):
                shutil.rmtree(target_path)
            else:
                os.remove(target_path)
            return jsonify({"status": "success", "message": "Item deleted successfully."})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/upload/<server_id>", methods=["POST"])
def upload_file(server_id):
    servers = load_servers()
    if server_id not in servers:
        return jsonify({"status": "error", "message": "Server not found."}), 404

    if 'file' not in request.files:
        return jsonify({"status": "error", "message": "No file uploaded."}), 400

    file = request.files['file']
    if not file or file.filename == '':
        return jsonify({"status": "error", "message": "Empty file name."}), 400

    server_dir = get_server_dir(server_id)
    sub_path = request.form.get("path", "").strip().lstrip("/\\")
    target_dir = os.path.normpath(os.path.join(server_dir, sub_path))

    if not is_safe_path(server_dir, target_dir) or not os.path.isdir(target_dir):
        return jsonify({"status": "error", "message": "Destination folder invalid."}), 400

    safe_name = secure_filename(file.filename) or f"upload_{uuid.uuid4().hex[:6]}"
    final_destination = os.path.join(target_dir, safe_name)

    try:
        file.save(final_destination)
        return jsonify({"status": "success", "message": f"'{safe_name}' uploaded successfully."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/create_folder/<server_id>", methods=["POST"])
def create_folder(server_id):
    servers = load_servers()
    if server_id not in servers:
        return jsonify({"status": "error", "message": "Server not found."}), 404

    data = request.get_json() or {}
    folder_name = secure_filename(data.get("folder_name", "").strip())
    sub_path = data.get("path", "").strip().lstrip("/\\")

    if not folder_name:
        return jsonify({"status": "error", "message": "Folder name is required."}), 400

    server_dir = get_server_dir(server_id)
    target_dir = os.path.normpath(os.path.join(server_dir, sub_path, folder_name))

    if not is_safe_path(server_dir, target_dir):
        return jsonify({"status": "error", "message": "Invalid folder path."}), 400

    if os.path.exists(target_dir):
        return jsonify({"status": "error", "message": "Folder already exists."}), 400

    try:
        os.makedirs(target_dir, exist_ok=True)
        return jsonify({"status": "success", "message": "Folder created successfully."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/rename/<server_id>", methods=["POST"])
def rename_item(server_id):
    servers = load_servers()
    if server_id not in servers:
        return jsonify({"status": "error", "message": "Server not found."}), 404

    data = request.get_json() or {}
    old_path_rel = data.get("old_path", "").strip().lstrip("/\\")
    new_name = secure_filename(data.get("new_name", "").strip())

    if not old_path_rel or not new_name:
        return jsonify({"status": "error", "message": "Old path and new name are required."}), 400

    server_dir = get_server_dir(server_id)
    old_full = os.path.normpath(os.path.join(server_dir, old_path_rel))

    if not is_safe_path(server_dir, old_full) or not os.path.exists(old_full):
        return jsonify({"status": "error", "message": "Source item does not exist."}), 404

    parent_dir = os.path.dirname(old_full)
    new_full = os.path.normpath(os.path.join(parent_dir, new_name))

    if not is_safe_path(server_dir, new_full):
        return jsonify({"status": "error", "message": "Invalid new path."}), 400

    if os.path.exists(new_full):
        return jsonify({"status": "error", "message": "An item with that name already exists."}), 400

    try:
        os.rename(old_full, new_full)
        return jsonify({"status": "success", "message": "Item renamed successfully."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/extract/<server_id>", methods=["POST"])
def extract_zip(server_id):
    servers = load_servers()
    if server_id not in servers:
        return jsonify({"status": "error", "message": "Server not found."}), 404

    data = request.get_json() or {}
    rel_path = data.get("path", "").strip().lstrip("/\\")
    server_dir = get_server_dir(server_id)
    archive_path = os.path.normpath(os.path.join(server_dir, rel_path))

    if not is_safe_path(server_dir, archive_path) or not os.path.isfile(archive_path):
        return jsonify({"status": "error", "message": "ZIP file not found."}), 404

    dest_dir = os.path.dirname(archive_path)

    try:
        with zipfile.ZipFile(archive_path, 'r') as zip_ref:
            # Safe extraction guard against zip-slip
            for member in zip_ref.namelist():
                member_target = os.path.normpath(os.path.join(dest_dir, member))
                if not is_safe_path(server_dir, member_target):
                    return jsonify({"status": "error", "message": "Security error: Unsafe zip content."}), 400
            zip_ref.extractall(dest_dir)

        return jsonify({"status": "success", "message": "ZIP archive extracted successfully."})
    except Exception as e:
        return jsonify({"status": "error", "message": f"Extraction failed: {str(e)}"}), 500

# -------------------------------------------------------------------------
# CONSOLE & LOG APIS
# -------------------------------------------------------------------------
@app.route("/api/logs/<server_id>", methods=["GET"])
def get_logs(server_id):
    servers = load_servers()
    if server_id not in servers:
        return jsonify({"status": "error", "message": "Server not found."}), 404

    server_dir = get_server_dir(server_id)
    log_path = os.path.join(server_dir, "output.log")

    if not os.path.exists(log_path):
        return jsonify({"status": "success", "logs": ""})

    try:
        # Read the last 250 KB to preserve responsiveness
        file_size = os.path.getsize(log_path)
        max_bytes = 250000
        with open(log_path, "rb") as f:
            if file_size > max_bytes:
                f.seek(file_size - max_bytes)
            logs = f.read().decode("utf-8", errors="replace")
        return jsonify({"status": "success", "logs": logs})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/clear_logs/<server_id>", methods=["POST"])
def clear_logs(server_id):
    servers = load_servers()
    if server_id not in servers:
        return jsonify({"status": "error", "message": "Server not found."}), 404

    server_dir = get_server_dir(server_id)
    log_path = os.path.join(server_dir, "output.log")

    try:
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Logs cleared.\n")
        return jsonify({"status": "success", "message": "Logs cleared successfully."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/command", methods=["POST"])
def send_command():
    data = request.get_json() or {}
    server_id = data.get("server_id", "")
    command = data.get("command", "").strip()

    if not server_id or not command:
        return jsonify({"status": "error", "message": "server_id and command are required."}), 400

    proc_info = RUNNING_PROCESSES.get(server_id)
    if not proc_info or proc_info["process"].poll() is not None:
        return jsonify({"status": "error", "message": "Server is not running. Cannot accept commands."}), 400

    proc = proc_info["process"]
    try:
        if proc.stdin and not proc.stdin.closed:
            proc.stdin.write(command + "\n")
            proc.stdin.flush()
            # Also append to log file for visual feedback in the console
            server_dir = get_server_dir(server_id)
            log_path = os.path.join(server_dir, "output.log")
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"\n> {command}\n")
            return jsonify({"status": "success", "message": "Command dispatched successfully."})
        else:
            return jsonify({"status": "error", "message": "Server standard input is not available."}), 400
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# -------------------------------------------------------------------------
# SETTINGS & CONFIGURATION APIS
# -------------------------------------------------------------------------
@app.route("/api/get_startup/<server_id>", methods=["GET"])
def get_startup(server_id):
    servers = load_servers()
    server = servers.get(server_id)
    if not server:
        return jsonify({"status": "error", "message": "Server not found."}), 404

    return jsonify({
        "status": "success",
        "startup_file": server.get("startup_file", "main.py"),
        "requirements_file": server.get("requirements_file", "requirements.txt")
    })

@app.route("/api/set_startup/<server_id>", methods=["POST"])
def set_startup(server_id):
    servers = load_servers()
    server = servers.get(server_id)
    if not server:
        return jsonify({"status": "error", "message": "Server not found."}), 404

    data = request.get_json() or {}
    startup_file = secure_filename(data.get("startup_file", "main.py").strip()) or "main.py"
    requirements_file = secure_filename(data.get("requirements_file", "requirements.txt").strip()) or "requirements.txt"

    server["startup_file"] = startup_file
    server["requirements_file"] = requirements_file
    save_servers(servers)

    return jsonify({"status": "success", "message": "Startup configurations saved successfully."})

@app.route("/api/server/<server_id>/settings", methods=["POST"])
def update_settings(server_id):
    servers = load_servers()
    server = servers.get(server_id)
    if not server:
        return jsonify({"status": "error", "message": "Server not found."}), 404

    data = request.get_json() or {}
    name = data.get("name", "").strip()
    startup_file = secure_filename(data.get("startup_file", "").strip())
    requirements_file = secure_filename(data.get("requirements_file", "").strip())

    if name:
        server["name"] = name
    if startup_file:
        server["startup_file"] = startup_file
    if requirements_file:
        server["requirements_file"] = requirements_file

    save_servers(servers)
    return jsonify({"status": "success", "message": "Server settings updated successfully.", "server": server})

# -------------------------------------------------------------------------
# MAIN ENTRYPOINT
# -------------------------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
