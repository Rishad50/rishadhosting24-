import os
import sys
import json
import uuid
import shutil
import zipfile
import threading
import subprocess
import time
import random
from datetime import datetime
from flask import Flask, render_template, request, jsonify
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100 MB Upload limit

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SERVERS_FILE = os.path.join(BASE_DIR, "servers.json")
BOTS_DIR = os.path.join(BASE_DIR, "bots")

os.makedirs(BOTS_DIR, exist_ok=True)

DATA_LOCK = threading.Lock()
RUNNING_PROCESSES = {}  # { server_id: {"process": Popen, "log_file": file_handle} }

# ----------------- HELPER FUNCTIONS -----------------
def load_servers():
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
    with DATA_LOCK:
        with open(SERVERS_FILE, "w", encoding="utf-8") as f:
            json.dump(servers, f, indent=4)

def get_server_dir(server_id):
    return os.path.join(BOTS_DIR, server_id)

def is_safe_path(base_dir, target_path):
    base_dir = os.path.abspath(base_dir)
    target_path = os.path.abspath(target_path)
    return target_path.startswith(base_dir)

def sync_server_process_statuses():
    servers = load_servers()
    modified = False

    for server_id, server in servers.items():
        proc_info = RUNNING_PROCESSES.get(server_id)
        if proc_info:
            poll = proc_info["process"].poll()
            if poll is not None:
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

# ----------------- HOME ROUTE -----------------
@app.route("/")
def home():
    servers = sync_server_process_statuses()
    return render_template("home.html", servers=servers)

# ----------------- SERVER CORE APIS -----------------
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

    # নাম + ঠিক ৭ ডিজিটের র‍্যান্ডম নম্বর
    clean_prefix = "".join(c for c in name.lower() if c.isalnum() or c == '_') or "server"
    random_number = random.randint(1000000, 9999999)
    server_id = f"{clean_prefix}_{random_number}"

    server_dir = get_server_dir(server_id)
    os.makedirs(server_dir, exist_ok=True)

    main_py_path = os.path.join(server_dir, "main.py")
    req_txt_path = os.path.join(server_dir, "requirements.txt")
    log_path = os.path.join(server_dir, "output.log")

    sample_main = (
        'import time\n'
        'import sys\n\n'
        'print("Starting server...", flush=True)\n'
        'time.sleep(1)\n'
        'print("Server started successfully!", flush=True)\n'
        'counter = 1\n'
        'while True:\n'
        '    print(f"[{time.strftime(\'%H:%M:%S\')}] Bot heartbeat #{counter} | Active", flush=True)\n'
        '    counter += 1\n'
        '    time.sleep(5)\n'
    )

    with open(main_py_path, "w", encoding="utf-8") as f:
        f.write(sample_main)
    with open(req_txt_path, "w", encoding="utf-8") as f:
        f.write("# Add python dependencies here\n")
    with open(log_path, "w", encoding="utf-8") as f:
        f.write(f"[{datetime.now().strftime('%H:%M:%S')}] Server created successfully.\n")

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
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
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
        return jsonify({"status": "error", "message": f"Startup file '{startup_file}' not found."}), 400

    log_path = os.path.join(server_dir, "output.log")
    log_file = open(log_path, "a", encoding="utf-8", buffering=1)
    timestamp = datetime.now().strftime("%H:%M:%S")
    log_file.write(f"\n[{timestamp}] Starting server...\n")
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
        return jsonify({"status": "error", "message": f"Execution error: {str(e)}"}), 500

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
            proc.wait(timeout=2)
        except (subprocess.TimeoutExpired, Exception):
            try:
                proc.kill()
            except Exception:
                pass
        try:
            if proc_info["log_file"] and not proc_info["log_file"].closed:
                proc_info["log_file"].write(f"[{datetime.now().strftime('%H:%M:%S')}] Server stopped.\n")
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
    stop_server(server_id)
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

# ----------------- LOGS & CONSOLE APIS -----------------
@app.route("/api/logs/<server_id>", methods=["GET"])
def get_logs(server_id):
    servers = load_servers()
    if server_id not in servers:
        return jsonify({"status": "error", "message": "Server not found."}), 404

    log_path = os.path.join(get_server_dir(server_id), "output.log")
    if not os.path.exists(log_path):
        return jsonify({"status": "success", "logs": ""})

    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        return jsonify({"status": "success", "logs": content})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/clear_logs/<server_id>", methods=["POST"])
def clear_logs(server_id):
    servers = load_servers()
    if server_id not in servers:
        return jsonify({"status": "error", "message": "Server not found."}), 404

    log_path = os.path.join(get_server_dir(server_id), "output.log")
    try:
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(f"[{datetime.now().strftime('%H:%M:%S')}] Logs cleared.\n")
        return jsonify({"status": "success", "message": "Logs cleared."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/command", methods=["POST"])
def send_command():
    data = request.get_json() or {}
    server_id = data.get("server_id", "")
    command = data.get("command", "").strip()

    if not server_id or not command:
        return jsonify({"status": "error", "message": "Missing command or server ID."}), 400

    proc_info = RUNNING_PROCESSES.get(server_id)
    if not proc_info or proc_info["process"].poll() is not None:
        return jsonify({"status": "error", "message": "Server is not currently running."}), 400

    proc = proc_info["process"]
    try:
        if proc.stdin and not proc.stdin.closed:
            proc.stdin.write(command + "\n")
            proc.stdin.flush()
            log_path = os.path.join(get_server_dir(server_id), "output.log")
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"> {command}\n")
            return jsonify({"status": "success", "message": "Command dispatched."})
        return jsonify({"status": "error", "message": "Process stdin is closed."}), 400
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# ----------------- FILE MANAGER APIS -----------------
@app.route("/api/files/<server_id>", methods=["GET"])
def list_files(server_id):
    servers = load_servers()
    if server_id not in servers:
        return jsonify({"status": "error", "message": "Server not found."}), 404

    server_dir = get_server_dir(server_id)
    sub_path = request.args.get("path", "").strip().lstrip("/\\")
    target_dir = os.path.normpath(os.path.join(server_dir, sub_path))

    if not is_safe_path(server_dir, target_dir) or not os.path.exists(target_dir):
        return jsonify({"status": "error", "message": "Invalid directory."}), 400

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
            return jsonify({"status": "error", "message": "File not found."}), 404
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
        target_path = os.path.normpath(os.path.join(server_dir, relative_path))

        if not is_safe_path(server_dir, target_path) or not os.path.exists(target_path):
            return jsonify({"status": "error", "message": "Item does not exist."}), 400

        try:
            if os.path.isdir(target_path):
                shutil.rmtree(target_path)
            else:
                os.remove(target_path)
            return jsonify({"status": "success", "message": "Item deleted."})
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
        return jsonify({"status": "error", "message": "Invalid directory."}), 400

    safe_name = secure_filename(file.filename) or f"upload_{uuid.uuid4().hex[:6]}"
    final_dest = os.path.join(target_dir, safe_name)

    try:
        file.save(final_dest)
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

    try:
        os.makedirs(target_dir, exist_ok=True)
        return jsonify({"status": "success", "message": "Folder created successfully."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

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
    return jsonify({"status": "success", "message": "Settings updated successfully."})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
