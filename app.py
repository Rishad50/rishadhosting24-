import os
import sys
import json
import uuid
import shutil
import zipfile
import threading
import subprocess
from datetime import datetime
from werkzeug.utils import secure_filename
from flask import Flask, render_template, request, jsonify, send_file

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50 MB max upload limit

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BOTS_DIR = os.path.join(BASE_DIR, "bots")
DATA_FILE = os.path.join(BASE_DIR, "servers.json")

os.makedirs(BOTS_DIR, exist_ok=True)

# Lock for data integrity
DATA_LOCK = threading.Lock()

# Store active process instances: { server_id: { "process": Popen, "log_file": file_obj, "started_at": str } }
RUNNING_PROCESSES = {}


# ==================== HELPER FUNCTIONS ====================

def load_servers():
    with DATA_LOCK:
        if not os.path.exists(DATA_FILE):
            with open(DATA_FILE, "w", encoding="utf-8") as f:
                json.dump({}, f, indent=4)
            return {}
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}


def save_servers(servers):
    with DATA_LOCK:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(servers, f, indent=4)


def sanitize_server_id(server_id):
    if not server_id or not isinstance(server_id, str):
        return None
    cleaned = "".join(c for c in server_id if c.isalnum() or c in "-_")
    return cleaned if cleaned == server_id else None


def get_safe_path(base_dir, relative_path=""):
    clean_rel = relative_path.lstrip("/\\")
    target_path = os.path.abspath(os.path.join(base_dir, clean_rel))
    if os.path.commonpath([base_dir, target_path]) != base_dir:
        return None
    return target_path


def sync_server_process(server_id, servers=None):
    if servers is None:
        servers = load_servers()

    server = servers.get(server_id)
    if not server:
        return None

    proc_info = RUNNING_PROCESSES.get(server_id)
    if proc_info:
        proc = proc_info.get("process")
        if proc and proc.poll() is not None:
            # Process terminated
            try:
                proc_info["log_file"].close()
            except Exception:
                pass
            del RUNNING_PROCESSES[server_id]
            server["status"] = "stopped"
            server["pid"] = None
            save_servers(servers)
    else:
        if server.get("status") == "running":
            server["status"] = "stopped"
            server["pid"] = None
            save_servers(servers)

    return server


def sync_all_servers():
    servers = load_servers()
    updated = False
    for s_id in list(servers.keys()):
        proc_info = RUNNING_PROCESSES.get(s_id)
        if proc_info:
            proc = proc_info.get("process")
            if proc and proc.poll() is not None:
                try:
                    proc_info["log_file"].close()
                except Exception:
                    pass
                del RUNNING_PROCESSES[s_id]
                servers[s_id]["status"] = "stopped"
                servers[s_id]["pid"] = None
                updated = True
        else:
            if servers[s_id].get("status") == "running":
                servers[s_id]["status"] = "stopped"
                servers[s_id]["pid"] = None
                updated = True
    if updated:
        save_servers(servers)
    return servers


# ==================== VIEW ROUTES ====================

@app.route("/")
def home():
    servers = sync_all_servers()
    return render_template("home.html", servers=servers)


# ==================== SERVER MANAGEMENT APIS ====================

@app.route("/api/servers", methods=["GET"])
def api_get_servers():
    servers = sync_all_servers()
    return jsonify({"status": "success", "servers": servers})


@app.route("/api/server/<server_id>", methods=["GET"])
def api_get_server(server_id):
    clean_id = sanitize_server_id(server_id)
    if not clean_id:
        return jsonify({"status": "error", "message": "Invalid Server ID"}), 400

    server = sync_server_process(clean_id)
    if not server:
        return jsonify({"status": "error", "message": "Server not found"}), 404

    return jsonify({"status": "success", "server": server})


@app.route("/api/create_server", methods=["POST"])
def api_create_server():
    data = request.get_json() or {}
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"status": "error", "message": "Server name is required"}), 400

    server_type = data.get("type", "Python")
    ram = data.get("ram", "1 GB")
    disk = data.get("disk", "1 GB")

    server_id = uuid.uuid4().hex[:10]
    server_dir = os.path.join(BOTS_DIR, server_id)
    os.makedirs(server_dir, exist_ok=True)

    # 1. Default main.py
    main_code = (
        'import time\n\n'
        'print("Server started successfully!")\n\n'
        'counter = 0\n\n'
        'while True:\n'
        '    counter += 1\n'
        '    print(f"Heartbeat #{counter} | Active")\n'
        '    time.sleep(10)\n'
    )
    with open(os.path.join(server_dir, "main.py"), "w", encoding="utf-8") as f:
        f.write(main_code)

    # 2. Default requirements.txt
    req_text = "# Add your pip packages here\n"
    with open(os.path.join(server_dir, "requirements.txt"), "w", encoding="utf-8") as f:
        f.write(req_text)

    # 3. Default output.log
    with open(os.path.join(server_dir, "output.log"), "w", encoding="utf-8") as f:
        f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Server created successfully.\n")

    # 4. Update servers.json
    servers = load_servers()
    new_server = {
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
    servers[server_id] = new_server
    save_servers(servers)

    return jsonify({"status": "success", "message": "Server created successfully!", "server": new_server})


@app.route("/api/start/<server_id>", methods=["POST"])
def api_start_server(server_id):
    clean_id = sanitize_server_id(server_id)
    if not clean_id:
        return jsonify({"status": "error", "message": "Invalid Server ID"}), 400

    servers = load_servers()
    server = sync_server_process(clean_id, servers)
    if not server:
        return jsonify({"status": "error", "message": "Server not found"}), 404

    if clean_id in RUNNING_PROCESSES:
        return jsonify({"status": "error", "message": "Server is already running!"}), 400

    server_dir = os.path.join(BOTS_DIR, clean_id)
    startup_file = server.get("startup_file", "main.py")
    startup_path = os.path.join(server_dir, startup_file)

    if not os.path.exists(startup_path):
        return jsonify({"status": "error", "message": f"Startup file '{startup_file}' not found."}), 404

    log_path = os.path.join(server_dir, "output.log")
    log_file = open(log_path, "a", encoding="utf-8", buffering=1)

    req_file = server.get("requirements_file", "requirements.txt")
    req_path = os.path.join(server_dir, req_file)
    if os.path.exists(req_path) and os.path.getsize(req_path) > 0:
        log_file.write(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Installing requirements...\n")
        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "-r", req_file],
                cwd=server_dir,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                timeout=60
            )
        except Exception as e:
            log_file.write(f"Pip installation error: {str(e)}\n")

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    log_file.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Starting {startup_file}...\n")

    try:
        proc = subprocess.Popen(
            [sys.executable, "-u", startup_file],
            cwd=server_dir,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env
        )
    except Exception as e:
        log_file.close()
        return jsonify({"status": "error", "message": f"Execution failed: {str(e)}"}), 500

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    RUNNING_PROCESSES[clean_id] = {
        "process": proc,
        "log_file": log_file,
        "started_at": now_str
    }

    server["status"] = "running"
    server["pid"] = proc.pid
    server["started_at"] = now_str
    save_servers(servers)

    return jsonify({"status": "success", "message": "Server started!", "pid": proc.pid})


@app.route("/api/stop/<server_id>", methods=["POST"])
def api_stop_server(server_id):
    clean_id = sanitize_server_id(server_id)
    if not clean_id:
        return jsonify({"status": "error", "message": "Invalid Server ID"}), 400

    servers = load_servers()
    server = servers.get(clean_id)
    if not server:
        return jsonify({"status": "error", "message": "Server not found"}), 404

    proc_info = RUNNING_PROCESSES.get(clean_id)
    if proc_info:
        proc = proc_info.get("process")
        try:
            proc.terminate()
            proc.wait(timeout=4)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

        try:
            proc_info["log_file"].write(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Process stopped by user.\n")
            proc_info["log_file"].close()
        except Exception:
            pass

        del RUNNING_PROCESSES[clean_id]

    server["status"] = "stopped"
    server["pid"] = None
    save_servers(servers)

    return jsonify({"status": "success", "message": "Server stopped successfully!"})


@app.route("/api/restart/<server_id>", methods=["POST"])
def api_restart_server(server_id):
    api_stop_server(server_id)
    return api_start_server(server_id)


@app.route("/api/server/<server_id>", methods=["DELETE"])
def api_delete_server(server_id):
    clean_id = sanitize_server_id(server_id)
    if not clean_id:
        return jsonify({"status": "error", "message": "Invalid Server ID"}), 400

    api_stop_server(clean_id)

    server_dir = os.path.join(BOTS_DIR, clean_id)
    if os.path.exists(server_dir):
        shutil.rmtree(server_dir, ignore_errors=True)

    servers = load_servers()
    if clean_id in servers:
        del servers[clean_id]
        save_servers(servers)

    return jsonify({"status": "success", "message": "Server deleted permanently!"})


# ==================== FILE MANAGER APIS ====================

@app.route("/api/files/<server_id>", methods=["GET"])
def api_list_files(server_id):
    clean_id = sanitize_server_id(server_id)
    if not clean_id:
        return jsonify({"status": "error", "message": "Invalid Server ID"}), 400

    server_dir = os.path.join(BOTS_DIR, clean_id)
    if not os.path.exists(server_dir):
        return jsonify({"status": "error", "message": "Server directory not found"}), 404

    subpath = request.args.get("path", "").strip()
    target_dir = get_safe_path(server_dir, subpath)
    if target_dir is None or not os.path.isdir(target_dir):
        return jsonify({"status": "error", "message": "Directory not found or access denied"}), 400

    items = []
    for entry in sorted(os.listdir(target_dir)):
        entry_path = os.path.join(target_dir, entry)
        is_dir = os.path.isdir(entry_path)
        try:
            stat = os.stat(entry_path)
            size = stat.st_size
            mtime = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
        except Exception:
            size = 0
            mtime = "-"

        items.append({
            "name": entry,
            "is_dir": is_dir,
            "size": size,
            "mtime": mtime
        })

    rel_breadcrumb = os.path.relpath(target_dir, server_dir).replace("\\", "/")
    if rel_breadcrumb == ".":
        rel_breadcrumb = ""

    return jsonify({"status": "success", "files": items, "current_path": rel_breadcrumb})


@app.route("/api/file/<server_id>", methods=["GET", "POST", "DELETE"])
def api_file_operations(server_id):
    clean_id = sanitize_server_id(server_id)
    if not clean_id:
        return jsonify({"status": "error", "message": "Invalid Server ID"}), 400

    server_dir = os.path.join(BOTS_DIR, clean_id)
    if not os.path.exists(server_dir):
        return jsonify({"status": "error", "message": "Server directory not found"}), 404

    if request.method == "GET":
        subpath = request.args.get("path", "")
        target_file = get_safe_path(server_dir, subpath)
        if not target_file or not os.path.isfile(target_file):
            return jsonify({"status": "error", "message": "File not found"}), 404

        try:
            with open(target_file, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            return jsonify({"status": "success", "content": content})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

    elif request.method == "POST":
        data = request.get_json() or {}
        subpath = data.get("path", "")
        content = data.get("content", "")
        target_file = get_safe_path(server_dir, subpath)
        if not target_file:
            return jsonify({"status": "error", "message": "Invalid path"}), 400

        try:
            with open(target_file, "w", encoding="utf-8") as f:
                f.write(content)
            return jsonify({"status": "success", "message": "File saved successfully!"})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

    elif request.method == "DELETE":
        subpath = request.args.get("path", "")
        target = get_safe_path(server_dir, subpath)
        if not target or target == server_dir:
            return jsonify({"status": "error", "message": "Cannot delete server root"}), 400

        if not os.path.exists(target):
            return jsonify({"status": "error", "message": "File or directory not found"}), 404

        try:
            if os.path.isdir(target):
                shutil.rmtree(target)
            else:
                os.remove(target)
            return jsonify({"status": "success", "message": "Deleted successfully!"})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/upload/<server_id>", methods=["POST"])
def api_upload_file(server_id):
    clean_id = sanitize_server_id(server_id)
    if not clean_id:
        return jsonify({"status": "error", "message": "Invalid Server ID"}), 400

    server_dir = os.path.join(BOTS_DIR, clean_id)
    subpath = request.form.get("path", "")
    target_dir = get_safe_path(server_dir, subpath)

    if not target_dir or not os.path.isdir(target_dir):
        return jsonify({"status": "error", "message": "Invalid upload destination"}), 400

    if 'file' not in request.files:
        return jsonify({"status": "error", "message": "No file uploaded"}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({"status": "error", "message": "No file selected"}), 400

    filename = secure_filename(file.filename)
    if not filename:
        return jsonify({"status": "error", "message": "Invalid filename"}), 400

    file.save(os.path.join(target_dir, filename))
    return jsonify({"status": "success", "message": f"'{filename}' uploaded successfully!"})


@app.route("/api/create_folder/<server_id>", methods=["POST"])
def api_create_folder(server_id):
    clean_id = sanitize_server_id(server_id)
    if not clean_id:
        return jsonify({"status": "error", "message": "Invalid Server ID"}), 400

    server_dir = os.path.join(BOTS_DIR, clean_id)
    data = request.get_json() or {}
    subpath = data.get("path", "")
    folder_name = secure_filename(data.get("name", "").strip())

    if not folder_name:
        return jsonify({"status": "error", "message": "Folder name is invalid"}), 400

    target_dir = get_safe_path(server_dir, subpath)
    if not target_dir or not os.path.isdir(target_dir):
        return jsonify({"status": "error", "message": "Target directory invalid"}), 400

    new_folder_path = os.path.join(target_dir, folder_name)
    try:
        os.makedirs(new_folder_path, exist_ok=False)
        return jsonify({"status": "success", "message": f"Folder '{folder_name}' created!"})
    except FileExistsError:
        return jsonify({"status": "error", "message": "Folder already exists!"}), 400
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/rename/<server_id>", methods=["POST"])
def api_rename(server_id):
    clean_id = sanitize_server_id(server_id)
    if not clean_id:
        return jsonify({"status": "error", "message": "Invalid Server ID"}), 400

    server_dir = os.path.join(BOTS_DIR, clean_id)
    data = request.get_json() or {}
    old_path_rel = data.get("old_path", "")
    new_name = secure_filename(data.get("new_name", "").strip())

    if not new_name:
        return jsonify({"status": "error", "message": "New name cannot be empty"}), 400

    old_target = get_safe_path(server_dir, old_path_rel)
    if not old_target or not os.path.exists(old_target) or old_target == server_dir:
        return jsonify({"status": "error", "message": "Invalid original file"}), 400

    parent_dir = os.path.dirname(old_target)
    new_target = os.path.join(parent_dir, new_name)

    try:
        os.rename(old_target, new_target)
        return jsonify({"status": "success", "message": "Renamed successfully!"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/extract/<server_id>", methods=["POST"])
def api_extract_zip(server_id):
    clean_id = sanitize_server_id(server_id)
    if not clean_id:
        return jsonify({"status": "error", "message": "Invalid Server ID"}), 400

    server_dir = os.path.join(BOTS_DIR, clean_id)
    data = request.get_json() or {}
    file_rel = data.get("path", "")
    target_file = get_safe_path(server_dir, file_rel)

    if not target_file or not os.path.isfile(target_file) or not target_file.endswith(".zip"):
        return jsonify({"status": "error", "message": "Valid ZIP file required"}), 400

    dest_dir = os.path.dirname(target_file)
    try:
        with zipfile.ZipFile(target_file, 'r') as zip_ref:
            # Safe zip extraction checking against path traversal
            for member in zip_ref.namelist():
                filename = os.path.basename(member)
                if not filename:
                    continue
                extracted_path = os.path.abspath(os.path.join(dest_dir, member))
                if os.path.commonpath([dest_dir, extracted_path]) == dest_dir:
                    zip_ref.extract(member, dest_dir)
        return jsonify({"status": "success", "message": "Extracted successfully!"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# ==================== CONSOLE APIS ====================

@app.route("/api/logs/<server_id>", methods=["GET"])
def api_get_logs(server_id):
    clean_id = sanitize_server_id(server_id)
    if not clean_id:
        return jsonify({"status": "error", "message": "Invalid Server ID"}), 400

    server_dir = os.path.join(BOTS_DIR, clean_id)
    log_path = os.path.join(server_dir, "output.log")
    if not os.path.exists(log_path):
        return jsonify({"status": "success", "logs": ""})

    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
            logs = "".join(lines[-300:])
        return jsonify({"status": "success", "logs": logs})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/clear_logs/<server_id>", methods=["POST"])
def api_clear_logs(server_id):
    clean_id = sanitize_server_id(server_id)
    if not clean_id:
        return jsonify({"status": "error", "message": "Invalid Server ID"}), 400

    server_dir = os.path.join(BOTS_DIR, clean_id)
    log_path = os.path.join(server_dir, "output.log")
    try:
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Logs cleared.\n")
        return jsonify({"status": "success", "message": "Logs cleared!"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/command", methods=["POST"])
def api_send_command():
    data = request.get_json() or {}
    server_id = sanitize_server_id(data.get("server_id"))
    command = data.get("command", "").strip()

    if not server_id:
        return jsonify({"status": "error", "message": "Invalid Server ID"}), 400
    if not command:
        return jsonify({"status": "error", "message": "Command is empty"}), 400

    server_dir = os.path.join(BOTS_DIR, server_id)
    log_path = os.path.join(server_dir, "output.log")

    proc_info = RUNNING_PROCESSES.get(server_id)
    if proc_info and proc_info.get("process") and proc_info["process"].poll() is None:
        proc = proc_info["process"]
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"\n> {command}\n")
            proc.stdin.write(command + "\n")
            proc.stdin.flush()
            return jsonify({"status": "success", "message": "Command sent to active process stdin"})
        except Exception as e:
            return jsonify({"status": "error", "message": f"Stdin pipe write error: {str(e)}"}), 500
    else:
        # If server is stopped, execute terminal command directly inside the server's directory
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"\n> {command}\n")
                res = subprocess.run(
                    command,
                    shell=True,
                    cwd=server_dir,
                    stdout=f,
                    stderr=subprocess.STDOUT,
                    timeout=15,
                    text=True
                )
            return jsonify({"status": "success", "message": "Command executed in server directory"})
        except subprocess.TimeoutExpired:
            return jsonify({"status": "error", "message": "Command timed out after 15s"}), 408
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500


# ==================== SETTINGS APIS ====================

@app.route("/api/get_startup/<server_id>", methods=["GET"])
def api_get_startup(server_id):
    clean_id = sanitize_server_id(server_id)
    if not clean_id:
        return jsonify({"status": "error", "message": "Invalid Server ID"}), 400

    servers = load_servers()
    server = servers.get(clean_id)
    if not server:
        return jsonify({"status": "error", "message": "Server not found"}), 404

    return jsonify({
        "status": "success",
        "name": server.get("name"),
        "startup_file": server.get("startup_file", "main.py"),
        "requirements_file": server.get("requirements_file", "requirements.txt")
    })


@app.route("/api/set_startup/<server_id>", methods=["POST"])
def api_set_startup(server_id):
    clean_id = sanitize_server_id(server_id)
    if not clean_id:
        return jsonify({"status": "error", "message": "Invalid Server ID"}), 400

    servers = load_servers()
    server = servers.get(clean_id)
    if not server:
        return jsonify({"status": "error", "message": "Server not found"}), 404

    data = request.get_json() or {}
    startup_file = secure_filename(data.get("startup_file", "main.py"))
    requirements_file = secure_filename(data.get("requirements_file", "requirements.txt"))

    if not startup_file:
        return jsonify({"status": "error", "message": "Startup file name invalid"}), 400

    server["startup_file"] = startup_file
    if requirements_file:
        server["requirements_file"] = requirements_file
    save_servers(servers)

    return jsonify({"status": "success", "message": "Startup configuration saved!"})


@app.route("/api/server/<server_id>/settings", methods=["POST"])
def api_update_settings(server_id):
    clean_id = sanitize_server_id(server_id)
    if not clean_id:
        return jsonify({"status": "error", "message": "Invalid Server ID"}), 400

    servers = load_servers()
    server = servers.get(clean_id)
    if not server:
        return jsonify({"status": "error", "message": "Server not found"}), 404

    data = request.get_json() or {}
    name = data.get("name", "").strip()
    startup_file = secure_filename(data.get("startup_file", "main.py"))
    requirements_file = secure_filename(data.get("requirements_file", "requirements.txt"))

    if not name:
        return jsonify({"status": "error", "message": "Server name cannot be empty"}), 400

    server["name"] = name
    server["startup_file"] = startup_file or "main.py"
    server["requirements_file"] = requirements_file or "requirements.txt"
    save_servers(servers)

    return jsonify({"status": "success", "message": "Server settings updated successfully!"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
