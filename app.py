import os
import sys
import json
import time
import zipfile
import subprocess
import threading
from datetime import datetime
from werkzeug.utils import secure_filename
from flask import Flask, render_template, request, jsonify, abort

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50 MB max upload limit

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DATA_FILE = os.path.join(BASE_DIR, "servers.json")
BOTS_DIR = os.path.join(BASE_DIR, "bots")

os.makedirs(BOTS_DIR, exist_ok=True)

# In-memory dictionary to hold running process handles: {server_id: {"process": Popen, "log_file": file_handle}}
RUNNING_PROCESSES = {}
PROCESS_LOCK = threading.Lock()

# -------------------------------------------------------------
# HELPER FUNCTIONS & STORAGE
# -------------------------------------------------------------

def load_servers():
    if not os.path.exists(DATA_FILE):
        return {}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_servers(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)

def get_server_dir(server_id):
    path = os.path.abspath(os.path.join(BOTS_DIR, server_id))
    os.makedirs(path, exist_ok=True)
    return path

def safe_path_join(base_dir, relative_path=""):
    clean_rel = relative_path.replace("\\", "/").lstrip("/")
    target = os.path.abspath(os.path.join(base_dir, clean_rel))
    if not (target == base_dir or target.startswith(base_dir + os.sep)):
        raise PermissionError("Access denied: Invalid path traversal detected.")
    return target

def format_file_size(bytes_size):
    if bytes_size < 1024:
        return f"{bytes_size} B"
    elif bytes_size < 1024 * 1024:
        return f"{bytes_size / 1024:.1f} KB"
    else:
        return f"{bytes_size / (1024 * 1024):.1f} MB"

def append_to_log(server_id, text):
    s_dir = get_server_dir(server_id)
    log_file = os.path.join(s_dir, "output.log")
    timestamp = datetime.now().strftime("[%H:%M:%S] ")
    with open(log_file, "a", encoding="utf-8", errors="replace") as f:
        for line in text.splitlines(True):
            if not line.startswith("["):
                f.write(timestamp + line)
            else:
                f.write(line)

def check_and_update_status(server_id):
    with PROCESS_LOCK:
        if server_id in RUNNING_PROCESSES:
            proc_info = RUNNING_PROCESSES[server_id]
            proc = proc_info.get("process")
            if proc and proc.poll() is not None:
                # Process exited
                try:
                    proc_info["log_file"].close()
                except Exception:
                    pass
                del RUNNING_PROCESSES[server_id]
                servers = load_servers()
                if server_id in servers:
                    servers[server_id]["status"] = "Stopped"
                    servers[server_id]["pid"] = None
                    save_servers(servers)
                append_to_log(server_id, f"[System] Process terminated with exit code {proc.returncode}.\n")

# -------------------------------------------------------------
# CORE PROCESS MANAGEMENT
# -------------------------------------------------------------

def start_server_process(server_id):
    check_and_update_status(server_id)
    servers = load_servers()
    if server_id not in servers:
        return False, "Server not found"

    with PROCESS_LOCK:
        if server_id in RUNNING_PROCESSES:
            return False, "Server is already running"

        s_info = servers[server_id]
        s_dir = get_server_dir(server_id)
        main_file = s_info.get("main_file", "main.py")
        req_file = s_info.get("requirements_file", "requirements.txt")

        target_script = os.path.join(s_dir, main_file)
        if not os.path.exists(target_script):
            return False, f"Startup file '{main_file}' not found."

        log_path = os.path.join(s_dir, "output.log")
        log_handle = open(log_path, "a", buffering=1, encoding="utf-8", errors="replace")

        # Pip install check if requirements file exists and has content
        req_path = os.path.join(s_dir, req_file)
        if os.path.exists(req_path) and os.path.getsize(req_path) > 0:
            log_handle.write(f"[{datetime.now().strftime('%H:%M:%S')}] Checking requirements in {req_file}...\n")
            try:
                subprocess.run(
                    [sys.executable, "-m", "pip", "install", "-r", req_path],
                    cwd=s_dir,
                    stdout=log_handle,
                    stderr=log_handle,
                    timeout=60
                )
            except Exception as e:
                log_handle.write(f"[{datetime.now().strftime('%H:%M:%S')}] [Pip Error] {str(e)}\n")

        log_handle.write(f"[{datetime.now().strftime('%H:%M:%S')}] Starting server: {sys.executable} {main_file}...\n")

        try:
            process = subprocess.Popen(
                [sys.executable, "-u", main_file],
                cwd=s_dir,
                stdin=subprocess.PIPE,
                stdout=log_handle,
                stderr=log_handle,
                text=True,
                bufsize=1
            )
            RUNNING_PROCESSES[server_id] = {
                "process": process,
                "log_file": log_handle
            }
            servers[server_id]["status"] = "Running"
            servers[server_id]["pid"] = process.pid
            save_servers(servers)
            return True, "Server started successfully"
        except Exception as e:
            log_handle.close()
            return False, f"Execution failed: {str(e)}"

def stop_server_process(server_id):
    check_and_update_status(server_id)
    with PROCESS_LOCK:
        if server_id not in RUNNING_PROCESSES:
            servers = load_servers()
            if server_id in servers:
                servers[server_id]["status"] = "Stopped"
                servers[server_id]["pid"] = None
                save_servers(servers)
            return True, "Server is already stopped"

        proc_info = RUNNING_PROCESSES[server_id]
        proc = proc_info.get("process")
        try:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        except Exception:
            pass

        try:
            proc_info["log_file"].close()
        except Exception:
            pass

        del RUNNING_PROCESSES[server_id]
        servers = load_servers()
        if server_id in servers:
            servers[server_id]["status"] = "Stopped"
            servers[server_id]["pid"] = None
            save_servers(servers)
            
        append_to_log(server_id, "[System] Server stopped by user.\n")
        return True, "Server stopped successfully"

# -------------------------------------------------------------
# ROUTES: PAGE
# -------------------------------------------------------------

@app.route('/')
def index():
    servers = load_servers()
    for sid in list(servers.keys()):
        check_and_update_status(sid)
    servers = load_servers()
    return render_template("home.html", servers=servers)

# -------------------------------------------------------------
# API: SERVER LIFE CYCLE
# -------------------------------------------------------------

@app.route('/api/create_server', methods=['POST'])
def api_create_server():
    data = request.get_json() or {}
    server_name = data.get("server_name", "").strip()
    if not server_name:
        return jsonify({"success": False, "error": "Server Name is required."}), 400

    server_id = f"srv_{int(time.time())}"
    s_dir = get_server_dir(server_id)

    # Initial main.py
    main_code = (
        "import time\n\n"
        "print(\"Server started successfully!\")\n\n"
        "counter = 0\n\n"
        "while True:\n"
        "    counter += 1\n"
        "    print(f\"Heartbeat #{counter} | Active\")\n"
        "    time.sleep(10)\n"
    )
    with open(os.path.join(s_dir, "main.py"), "w", encoding="utf-8") as f:
        f.write(main_code)

    # Initial requirements.txt
    with open(os.path.join(s_dir, "requirements.txt"), "w", encoding="utf-8") as f:
        f.write("")

    # Initial output.log
    with open(os.path.join(s_dir, "output.log"), "w", encoding="utf-8") as f:
        f.write(f"[{datetime.now().strftime('%H:%M:%S')}] Server created successfully.\n")

    servers = load_servers()
    servers[server_id] = {
        "id": server_id,
        "name": server_name,
        "type": "Python",
        "ram": data.get("ram", "1 GB"),
        "disk": data.get("disk", "1 GB"),
        "status": "Stopped",
        "created": datetime.now().strftime("%b %d, %Y %H:%M"),
        "main_file": "main.py",
        "requirements_file": "requirements.txt",
        "pid": None
    }
    save_servers(servers)

    return jsonify({"success": True, "server_id": server_id, "server": servers[server_id]})

@app.route('/api/start/<server_id>', methods=['POST'])
def api_start(server_id):
    success, msg = start_server_process(server_id)
    return jsonify({"success": success, "message": msg})

@app.route('/api/stop/<server_id>', methods=['POST'])
def api_stop(server_id):
    success, msg = stop_server_process(server_id)
    return jsonify({"success": success, "message": msg})

@app.route('/api/restart/<server_id>', methods=['POST'])
def api_restart(server_id):
    stop_server_process(server_id)
    time.sleep(0.5)
    success, msg = start_server_process(server_id)
    return jsonify({"success": success, "message": "Server restarted successfully" if success else msg})

# -------------------------------------------------------------
# API: LOGS & CONSOLE COMMAND
# -------------------------------------------------------------

@app.route('/api/logs/<server_id>', methods=['GET'])
def api_logs(server_id):
    check_and_update_status(server_id)
    servers = load_servers()
    if server_id not in servers:
        return jsonify({"success": False, "error": "Server not found"}), 404

    s_dir = get_server_dir(server_id)
    log_file = os.path.join(s_dir, "output.log")
    logs = ""
    if os.path.exists(log_file):
        try:
            with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                logs = f.read()
        except Exception as e:
            logs = f"Error reading logs: {str(e)}"

    return jsonify({
        "success": True,
        "logs": logs,
        "status": servers[server_id].get("status", "Stopped")
    })

@app.route('/api/clear_logs/<server_id>', methods=['POST'])
def api_clear_logs(server_id):
    s_dir = get_server_dir(server_id)
    log_file = os.path.join(s_dir, "output.log")
    try:
        with open(log_file, "w", encoding="utf-8") as f:
            f.write(f"[{datetime.now().strftime('%H:%M:%S')}] Logs cleared.\n")
        return jsonify({"success": True, "message": "Logs cleared"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/command', methods=['POST'])
def api_command():
    data = request.get_json() or {}
    server_id = data.get("server_id")
    command = data.get("command", "").strip()

    if not server_id or not command:
        return jsonify({"success": False, "error": "Missing parameters"}), 400

    check_and_update_status(server_id)
    s_dir = get_server_dir(server_id)

    # 1. If server process is active, send to stdin
    with PROCESS_LOCK:
        if server_id in RUNNING_PROCESSES:
            proc = RUNNING_PROCESSES[server_id].get("process")
            if proc and proc.stdin:
                try:
                    proc.stdin.write(command + "\n")
                    proc.stdin.flush()
                    append_to_log(server_id, f"> {command}\n")
                    return jsonify({"success": True, "message": "Command sent to running process stdin"})
                except Exception as e:
                    return jsonify({"success": False, "error": f"Stdin error: {str(e)}"}), 500

    # 2. Server is stopped: execute safely inside the server's directory
    append_to_log(server_id, f"$ {command}\n")
    try:
        res = subprocess.run(
            command,
            shell=True,
            cwd=s_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15
        )
        output = res.stdout + res.stderr
        if output:
            append_to_log(server_id, output)
        return jsonify({"success": True, "message": "Command executed"})
    except subprocess.TimeoutExpired:
        append_to_log(server_id, "[Error] Command timed out after 15 seconds.\n")
        return jsonify({"success": False, "error": "Command timed out"}), 408
    except Exception as e:
        append_to_log(server_id, f"[Error] {str(e)}\n")
        return jsonify({"success": False, "error": str(e)}), 500

# -------------------------------------------------------------
# API: FILE MANAGER
# -------------------------------------------------------------

@app.route('/api/files/<server_id>', methods=['GET'])
def api_files(server_id):
    s_dir = get_server_dir(server_id)
    req_sub_path = request.args.get('path', '').strip()

    try:
        target_dir = safe_path_join(s_dir, req_sub_path)
    except PermissionError as e:
        return jsonify({"success": False, "error": str(e)}), 403

    if not os.path.exists(target_dir) or not os.path.isdir(target_dir):
        return jsonify({"success": False, "error": "Directory not found"}), 404

    items = []
    try:
        for entry in os.scandir(target_dir):
            stat = entry.stat()
            mod_time = datetime.fromtimestamp(stat.st_mtime).strftime("%b %d, %H:%M")
            is_dir = entry.is_dir()
            items.append({
                "name": entry.name,
                "is_dir": is_dir,
                "size": "-" if is_dir else format_file_size(stat.st_size),
                "modified": mod_time
            })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

    # Folders first, then sorted by name
    items.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
    
    # Calculate relative current path
    rel_path = os.path.relpath(target_dir, s_dir).replace("\\", "/")
    if rel_path == ".":
        rel_path = ""

    return jsonify({"success": True, "current_path": rel_path, "items": items})

@app.route('/api/file/<server_id>', methods=['GET'])
def api_get_file(server_id):
    s_dir = get_server_dir(server_id)
    rel_path = request.args.get('path', '').strip()
    try:
        target_file = safe_path_join(s_dir, rel_path)
    except PermissionError as e:
        return jsonify({"success": False, "error": str(e)}), 403

    if not os.path.exists(target_file) or os.path.isdir(target_file):
        return jsonify({"success": False, "error": "File not found"}), 404

    try:
        with open(target_file, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        return jsonify({"success": True, "content": content, "filename": os.path.basename(target_file)})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/file/<server_id>', methods=['POST'])
def api_save_file(server_id):
    s_dir = get_server_dir(server_id)
    data = request.get_json() or {}
    rel_path = data.get("path", "").strip()
    content = data.get("content", "")

    try:
        target_file = safe_path_join(s_dir, rel_path)
    except PermissionError as e:
        return jsonify({"success": False, "error": str(e)}), 403

    try:
        os.makedirs(os.path.dirname(target_file), exist_ok=True)
        with open(target_file, "w", encoding="utf-8") as f:
            f.write(content)
        return jsonify({"success": True, "message": "File saved successfully"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/file/<server_id>', methods=['DELETE'])
def api_delete_file(server_id):
    s_dir = get_server_dir(server_id)
    data = request.get_json() or {}
    rel_path = data.get("path", "").strip()

    try:
        target = safe_path_join(s_dir, rel_path)
    except PermissionError as e:
        return jsonify({"success": False, "error": str(e)}), 403

    if target == s_dir:
        return jsonify({"success": False, "error": "Cannot delete server root directory"}), 400

    if not os.path.exists(target):
        return jsonify({"success": False, "error": "Target does not exist"}), 404

    try:
        if os.path.isdir(target):
            import shutil
            shutil.rmtree(target)
        else:
            os.remove(target)
        return jsonify({"success": True, "message": "Deleted successfully"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/upload/<server_id>', methods=['POST'])
def api_upload(server_id):
    s_dir = get_server_dir(server_id)
    rel_path = request.form.get("path", "").strip()

    try:
        target_dir = safe_path_join(s_dir, rel_path)
    except PermissionError as e:
        return jsonify({"success": False, "error": str(e)}), 403

    if 'file' not in request.files:
        return jsonify({"success": False, "error": "No file uploaded"}), 400

    uploaded_file = request.files['file']
    if uploaded_file.filename == '':
        return jsonify({"success": False, "error": "Empty filename"}), 400

    safe_name = secure_filename(uploaded_file.filename)
    if not safe_name:
        return jsonify({"success": False, "error": "Invalid filename"}), 400

    os.makedirs(target_dir, exist_ok=True)
    destination = os.path.join(target_dir, safe_name)
    uploaded_file.save(destination)

    return jsonify({"success": True, "message": f"Uploaded '{safe_name}' successfully"})

@app.route('/api/create_folder/<server_id>', methods=['POST'])
def api_create_folder(server_id):
    s_dir = get_server_dir(server_id)
    data = request.get_json() or {}
    rel_path = data.get("path", "").strip()
    folder_name = secure_filename(data.get("folder_name", "").strip())

    if not folder_name:
        return jsonify({"success": False, "error": "Invalid folder name"}), 400

    try:
        target_dir = safe_path_join(s_dir, os.path.join(rel_path, folder_name))
        os.makedirs(target_dir, exist_ok=True)
        return jsonify({"success": True, "message": "Folder created successfully"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/rename/<server_id>', methods=['POST'])
def api_rename(server_id):
    s_dir = get_server_dir(server_id)
    data = request.get_json() or {}
    old_path = data.get("old_path", "").strip()
    new_name = secure_filename(data.get("new_name", "").strip())

    if not new_name:
        return jsonify({"success": False, "error": "Invalid new name"}), 400

    try:
        source = safe_path_join(s_dir, old_path)
        dest_dir = os.path.dirname(source)
        dest = os.path.join(dest_dir, new_name)
        
        if os.path.exists(dest):
            return jsonify({"success": False, "error": "File or folder with this name already exists"}), 400

        os.rename(source, dest)
        return jsonify({"success": True, "message": "Renamed successfully"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/extract/<server_id>', methods=['POST'])
def api_extract(server_id):
    s_dir = get_server_dir(server_id)
    data = request.get_json() or {}
    rel_path = data.get("path", "").strip()

    try:
        zip_path = safe_path_join(s_dir, rel_path)
    except PermissionError as e:
        return jsonify({"success": False, "error": str(e)}), 403

    if not os.path.exists(zip_path) or not zip_path.lower().endswith(".zip"):
        return jsonify({"success": False, "error": "Not a valid ZIP file"}), 400

    target_dir = os.path.dirname(zip_path)

    try:
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            # Zip Slip protection
            for member in zip_ref.namelist():
                member_path = os.path.abspath(os.path.join(target_dir, member))
                if not (member_path == target_dir or member_path.startswith(target_dir + os.sep)):
                    return jsonify({"success": False, "error": "Malicious ZIP content detected"}), 400
            zip_ref.extractall(target_dir)
        return jsonify({"success": True, "message": "ZIP extracted successfully"})
    except Exception as e:
        return jsonify({"success": False, "error": f"Extraction error: {str(e)}"}), 500

# -------------------------------------------------------------
# API: SETTINGS (STARTUP CONFIG)
# -------------------------------------------------------------

@app.route('/api/get_startup/<server_id>', methods=['GET'])
def api_get_startup(server_id):
    servers = load_servers()
    if server_id not in servers:
        return jsonify({"success": False, "error": "Server not found"}), 404

    s = servers[server_id]
    return jsonify({
        "success": True,
        "server_id": s["id"],
        "main_file": s.get("main_file", "main.py"),
        "requirements_file": s.get("requirements_file", "requirements.txt")
    })

@app.route('/api/set_startup/<server_id>', methods=['POST'])
def api_set_startup(server_id):
    servers = load_servers()
    if server_id not in servers:
        return jsonify({"success": False, "error": "Server not found"}), 404

    data = request.get_json() or {}
    main_file = secure_filename(data.get("main_file", "main.py").strip()) or "main.py"
    req_file = secure_filename(data.get("requirements_file", "requirements.txt").strip()) or "requirements.txt"

    servers[server_id]["main_file"] = main_file
    servers[server_id]["requirements_file"] = req_file
    save_servers(servers)

    return jsonify({"success": True, "message": "Startup configuration saved successfully"})

# -------------------------------------------------------------
# RUN
# -------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
