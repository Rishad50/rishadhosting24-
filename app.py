import os
import sys
import re
import json
import random
import shutil
import zipfile
import threading
import subprocess
from datetime import datetime
from werkzeug.utils import secure_filename
from flask import Flask, render_template, request, jsonify

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BOTS_DIR = os.path.join(BASE_DIR, "bots")
DATA_FILE = os.path.join(BASE_DIR, "servers.json")

os.makedirs(BOTS_DIR, exist_ok=True)
DATA_LOCK = threading.Lock()
RUNNING_PROCESSES = {}


# ==================== HELPERS ====================

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
    cleaned = re.sub(r'[^a-zA-Z0-9_\-]', '', server_id)
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


# ==================== MAIN ROUTES ====================

@app.route("/")
def index():
    servers = sync_all_servers()
    return render_template("home.html", servers=servers)


@app.route("/api/servers", methods=["GET"])
def api_get_servers():
    servers = sync_all_servers()
    return jsonify({"status": "success", "servers": servers})


@app.route("/api/server/<server_id>", methods=["GET"])
def api_get_server(server_id):
    clean_id = sanitize_server_id(server_id)
    if not clean_id:
        return jsonify({"status": "error", "message": "Invalid ID"}), 400

    server = sync_server_process(clean_id)
    if not server:
        return jsonify({"status": "error", "message": "Server not found"}), 404

    return jsonify({"status": "success", "server": server})


@app.route("/api/create_server", methods=["POST"])
def api_create_server():
    data = request.get_json() or {}
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"status": "error", "message": "Server Name is required!"}), 400

    # ID format matching: testing_1779785743
    name_clean = re.sub(r'[^a-zA-Z0-9]', '', name).lower() or "server"
    random_digits = "".join([str(random.randint(0, 9)) for _ in range(10)])
    server_id = f"{name_clean}_{random_digits}"

    server_dir = os.path.join(BOTS_DIR, server_id)
    os.makedirs(server_dir, exist_ok=True)

    # 1. Default main.py
    main_code = (
        'import time\n\n'
        f'print("Starting {name}...")\n'
        'counter = 0\n'
        'while True:\n'
        '    counter += 1\n'
        '    print(f"[{time.strftime(\'%H:%M:%S\')}] Heartbeat #{counter} - Server online.")\n'
        '    time.sleep(10)\n'
    )
    with open(os.path.join(server_dir, "main.py"), "w", encoding="utf-8") as f:
        f.write(main_code)

    # 2. requirements.txt
    with open(os.path.join(server_dir, "requirements.txt"), "w", encoding="utf-8") as f:
        f.write("# Add pip dependencies here\n")

    # 3. output.log
    with open(os.path.join(server_dir, "output.log"), "w", encoding="utf-8") as f:
        f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Server created successfully.\n")

    servers = load_servers()
    new_server = {
        "id": server_id,
        "name": name,
        "type": "Python",
        "status": "stopped",
        "pid": None,
        "startup_file": "main.py",
        "requirements_file": "requirements.txt",
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M")
    }
    servers[server_id] = new_server
    save_servers(servers)

    return jsonify({"status": "success", "message": "Server created successfully!", "server": new_server})


@app.route("/api/start/<server_id>", methods=["POST"])
def api_start_server(server_id):
    clean_id = sanitize_server_id(server_id)
    servers = load_servers()
    server = sync_server_process(clean_id, servers)
    if not server:
        return jsonify({"status": "error", "message": "Server not found"}), 404

    if clean_id in RUNNING_PROCESSES:
        return jsonify({"status": "error", "message": "Server already running"}), 400

    server_dir = os.path.join(BOTS_DIR, clean_id)
    startup_file = server.get("startup_file", "main.py")
    startup_path = os.path.join(server_dir, startup_file)

    if not os.path.exists(startup_path):
        return jsonify({"status": "error", "message": f"'{startup_file}' not found"}), 404

    log_path = os.path.join(server_dir, "output.log")
    log_file = open(log_path, "a", encoding="utf-8", buffering=1)

    req_file = server.get("requirements_file", "requirements.txt")
    req_path = os.path.join(server_dir, req_file)
    if os.path.exists(req_path) and os.path.getsize(req_path) > 5:
        log_file.write(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Installing dependencies...\n")
        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "-r", req_file],
                cwd=server_dir,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                timeout=45
            )
        except Exception as e:
            log_file.write(f"Pip error: {str(e)}\n")

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

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
        return jsonify({"status": "error", "message": f"Launch failed: {str(e)}"}), 500

    RUNNING_PROCESSES[clean_id] = {
        "process": proc,
        "log_file": log_file,
        "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }

    server["status"] = "running"
    server["pid"] = proc.pid
    save_servers(servers)

    return jsonify({"status": "success", "message": "Server started successfully!"})


@app.route("/api/stop/<server_id>", methods=["POST"])
def api_stop_server(server_id):
    clean_id = sanitize_server_id(server_id)
    servers = load_servers()
    server = servers.get(clean_id)
    if not server:
        return jsonify({"status": "error", "message": "Server not found"}), 404

    proc_info = RUNNING_PROCESSES.get(clean_id)
    if proc_info:
        proc = proc_info.get("process")
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            proc_info["log_file"].write(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Server stopped.\n")
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
    api_stop_server(clean_id)

    server_dir = os.path.join(BOTS_DIR, clean_id)
    if os.path.exists(server_dir):
        shutil.rmtree(server_dir, ignore_errors=True)

    servers = load_servers()
    if clean_id in servers:
        del servers[clean_id]
        save_servers(servers)

    return jsonify({"status": "success", "message": "Server deleted successfully!"})


# ==================== FILE MANAGER ====================

@app.route("/api/files/<server_id>", methods=["GET"])
def api_list_files(server_id):
    clean_id = sanitize_server_id(server_id)
    server_dir = os.path.join(BOTS_DIR, clean_id)
    subpath = request.args.get("path", "").strip()
    target_dir = get_safe_path(server_dir, subpath)

    if not target_dir or not os.path.isdir(target_dir):
        return jsonify({"status": "error", "message": "Invalid directory"}), 400

    items = []
    for entry in sorted(os.listdir(target_dir)):
        entry_path = os.path.join(target_dir, entry)
        is_dir = os.path.isdir(entry_path)
        try:
            stat = os.stat(entry_path)
            size = stat.st_size
            mtime = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
        except Exception:
            size, mtime = 0, "-"

        items.append({"name": entry, "is_dir": is_dir, "size": size, "mtime": mtime})

    rel = os.path.relpath(target_dir, server_dir).replace("\\", "/")
    return jsonify({"status": "success", "files": items, "current_path": "" if rel == "." else rel})


@app.route("/api/file/<server_id>", methods=["GET", "POST", "DELETE"])
def api_file_action(server_id):
    clean_id = sanitize_server_id(server_id)
    server_dir = os.path.join(BOTS_DIR, clean_id)

    if request.method == "GET":
        target = get_safe_path(server_dir, request.args.get("path", ""))
        if not target or not os.path.isfile(target):
            return jsonify({"status": "error", "message": "File not found"}), 404
        try:
            with open(target, "r", encoding="utf-8", errors="replace") as f:
                return jsonify({"status": "success", "content": f.read()})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

    elif request.method == "POST":
        data = request.get_json() or {}
        target = get_safe_path(server_dir, data.get("path", ""))
        if not target:
            return jsonify({"status": "error", "message": "Invalid path"}), 400
        try:
            with open(target, "w", encoding="utf-8") as f:
                f.write(data.get("content", ""))
            return jsonify({"status": "success", "message": "File saved!"})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

    elif request.method == "DELETE":
        target = get_safe_path(server_dir, request.args.get("path", ""))
        if not target or target == server_dir:
            return jsonify({"status": "error", "message": "Cannot delete server root"}), 400
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
    server_dir = os.path.join(BOTS_DIR, clean_id)
    target_dir = get_safe_path(server_dir, request.form.get("path", ""))

    if not target_dir or not os.path.isdir(target_dir) or 'file' not in request.files:
        return jsonify({"status": "error", "message": "Upload error"}), 400

    file = request.files['file']
    filename = secure_filename(file.filename)
    if not filename:
        return jsonify({"status": "error", "message": "Invalid filename"}), 400

    file.save(os.path.join(target_dir, filename))
    return jsonify({"status": "success", "message": f"'{filename}' uploaded!"})


@app.route("/api/create_folder/<server_id>", methods=["POST"])
def api_create_folder(server_id):
    clean_id = sanitize_server_id(server_id)
    server_dir = os.path.join(BOTS_DIR, clean_id)
    data = request.get_json() or {}
    folder_name = secure_filename(data.get("name", "").strip())
    target_dir = get_safe_path(server_dir, data.get("path", ""))

    if not folder_name or not target_dir:
        return jsonify({"status": "error", "message": "Invalid folder name"}), 400

    try:
        os.makedirs(os.path.join(target_dir, folder_name), exist_ok=False)
        return jsonify({"status": "success", "message": f"Folder '{folder_name}' created!"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# ==================== CONSOLE ====================

@app.route("/api/logs/<server_id>", methods=["GET"])
def api_logs(server_id):
    clean_id = sanitize_server_id(server_id)
    log_path = os.path.join(BOTS_DIR, clean_id, "output.log")
    if not os.path.exists(log_path):
        return jsonify({"status": "success", "logs": ""})
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            return jsonify({"status": "success", "logs": "".join(f.readlines()[-200:])})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/clear_logs/<server_id>", methods=["POST"])
def api_clear_logs(server_id):
    clean_id = sanitize_server_id(server_id)
    log_path = os.path.join(BOTS_DIR, clean_id, "output.log")
    try:
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Console cleared.\n")
        return jsonify({"status": "success", "message": "Cleared!"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/command", methods=["POST"])
def api_command():
    data = request.get_json() or {}
    server_id = sanitize_server_id(data.get("server_id"))
    cmd = data.get("command", "").strip()

    if not server_id or not cmd:
        return jsonify({"status": "error", "message": "Invalid request"}), 400

    server_dir = os.path.join(BOTS_DIR, server_id)
    log_path = os.path.join(server_dir, "output.log")

    proc_info = RUNNING_PROCESSES.get(server_id)
    if proc_info and proc_info.get("process") and proc_info["process"].poll() is None:
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"\n> {cmd}\n")
            proc_info["process"].stdin.write(cmd + "\n")
            proc_info["process"].stdin.flush()
            return jsonify({"status": "success", "message": "Piped to stdin"})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500
    else:
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"\n> {cmd}\n")
                subprocess.run(cmd, shell=True, cwd=server_dir, stdout=f, stderr=subprocess.STDOUT, timeout=10)
            return jsonify({"status": "success", "message": "Executed"})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500


# ==================== SETTINGS ====================

@app.route("/api/server/<server_id>/settings", methods=["POST"])
def api_update_settings(server_id):
    clean_id = sanitize_server_id(server_id)
    servers = load_servers()
    server = servers.get(clean_id)
    if not server:
        return jsonify({"status": "error", "message": "Server not found"}), 404

    data = request.get_json() or {}
    name = data.get("name", "").strip()
    startup = secure_filename(data.get("startup_file", "main.py"))
    reqs = secure_filename(data.get("requirements_file", "requirements.txt"))

    if not name:
        return jsonify({"status": "error", "message": "Server name required"}), 400

    server["name"] = name
    server["startup_file"] = startup or "main.py"
    server["requirements_file"] = reqs or "requirements.txt"
    save_servers(servers)

    return jsonify({"status": "success", "message": "Settings updated!"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
