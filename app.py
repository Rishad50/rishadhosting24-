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
app.config['MAX_CONTENT_LENGTH'] = 64 * 1024 * 1024  # 64 MB upload limit

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SERVERS_FILE = os.path.join(BASE_DIR, 'servers.json')
BOTS_DIR = os.path.join(BASE_DIR, 'bots')

# In-memory tracking of active subprocesses and file handles
# { server_id: { "process": subprocess.Popen, "log_file": file_handle } }
active_processes = {}
process_lock = threading.Lock()

# -------------------------------------------------------------------------
# Storage & Initialization Helpers
# -------------------------------------------------------------------------

def ensure_environment():
    """Ensure bots directory and servers.json exist."""
    if not os.path.exists(BOTS_DIR):
        os.makedirs(BOTS_DIR, exist_ok=True)
    if not os.path.exists(SERVERS_FILE):
        with open(SERVERS_FILE, 'w', encoding='utf-8') as f:
            json.dump([], f, indent=2)

def load_servers():
    """Load servers list from servers.json and synchronize process states."""
    ensure_environment()
    try:
        with open(SERVERS_FILE, 'r', encoding='utf-8') as f:
            servers = json.load(f)
    except Exception:
        servers = []

    # Sync status against real processes
    updated = False
    for s in servers:
        sid = s.get('id')
        with process_lock:
            proc_entry = active_processes.get(sid)
            if proc_entry:
                proc = proc_entry.get('process')
                if proc.poll() is not None:
                    # Process died or exited
                    s['status'] = 'stopped'
                    s['pid'] = None
                    try:
                        proc_entry['log_file'].close()
                    except Exception:
                        pass
                    del active_processes[sid]
                    updated = True
                else:
                    s['status'] = 'running'
                    s['pid'] = proc.pid
            else:
                if s.get('status') == 'running':
                    s['status'] = 'stopped'
                    s['pid'] = None
                    updated = True

    if updated:
        save_servers(servers)
    return servers

def save_servers(servers):
    """Write servers list to servers.json."""
    ensure_environment()
    with open(SERVERS_FILE, 'w', encoding='utf-8') as f:
        json.dump(servers, f, indent=2)

def get_server_by_id(server_id):
    servers = load_servers()
    for s in servers:
        if s.get('id') == server_id:
            return s, servers
    return None, servers

def get_server_directory(server_id):
    """Get the sanitized folder path for a server."""
    # Restrict server_id to safe alphanumeric, hyphens, and underscores
    safe_id = "".join(c for c in server_id if c.isalnum() or c in ('-', '_'))
    if not safe_id:
        return None
    folder = os.path.abspath(os.path.join(BOTS_DIR, safe_id))
    # Prevent traversal
    if not folder.startswith(os.path.abspath(BOTS_DIR)):
        return None
    return folder

def resolve_safe_path(base_dir, relative_path=""):
    """Safely resolve path within base_dir to block directory traversal."""
    if not relative_path:
        return base_dir
    cleaned = relative_path.lstrip("/\\")
    full_path = os.path.abspath(os.path.join(base_dir, cleaned))
    if not (full_path == base_dir or full_path.startswith(base_dir + os.sep)):
        return None
    return full_path

def format_file_size(bytes_size):
    """Convert bytes to readable units."""
    if bytes_size < 1024:
        return f"{bytes_size} B"
    elif bytes_size < 1024 * 1024:
        return f"{bytes_size / 1024:.1f} KB"
    elif bytes_size < 1024 * 1024 * 1024:
        return f"{bytes_size / (1024 * 1024):.1f} MB"
    return f"{bytes_size / (1024 * 1024 * 1024):.1f} GB"

# -------------------------------------------------------------------------
# Server Management APIs
# -------------------------------------------------------------------------

@app.route('/')
def index():
    return render_template('home.html')

@app.route('/api/servers', methods=['GET'])
def api_servers():
    servers = load_servers()
    return jsonify({"status": "success", "servers": servers})

@app.route('/api/server/<server_id>', methods=['GET'])
def api_get_server(server_id):
    server, _ = get_server_by_id(server_id)
    if not server:
        return jsonify({"status": "error", "message": "Server not found"}), 404
    return jsonify({"status": "success", "server": server})

@app.route('/api/create_server', methods=['POST'])
def api_create_server():
    data = request.get_json() or {}
    name = data.get('name', '').strip()
    server_type = data.get('type', 'Python').strip()
    ram = data.get('ram', '1 GB').strip()
    disk = data.get('disk', '2 GB').strip()

    if not name:
        return jsonify({"status": "error", "message": "Server name is required"}), 400

    server_id = f"srv-{uuid.uuid4().hex[:8]}"
    server_dir = get_server_directory(server_id)
    os.makedirs(server_dir, exist_ok=True)

    # Initial bot boilerplate files
    main_py_path = os.path.join(server_dir, 'main.py')
    if not os.path.exists(main_py_path):
        with open(main_py_path, 'w', encoding='utf-8') as f:
            f.write(
                'import time\n'
                'import sys\n\n'
                'print("=== Server started successfully ===", flush=True)\n'
                'count = 1\n'
                'try:\n'
                '    while True:\n'
                '        print(f"[{time.strftime(\'%Y-%m-%d %H:%M:%S\')}] Heartbeat #{count}", flush=True)\n'
                '        count += 1\n'
                '        time.sleep(5)\n'
                'except KeyboardInterrupt:\n'
                '    print("Server shutting down cleanly...", flush=True)\n'
            )

    req_path = os.path.join(server_dir, 'requirements.txt')
    if not os.path.exists(req_path):
        with open(req_path, 'w', encoding='utf-8') as f:
            f.write("# Add python dependencies here\n")

    log_path = os.path.join(server_dir, 'output.log')
    if not os.path.exists(log_path):
        with open(log_path, 'w', encoding='utf-8') as f:
            f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Server created.\n")

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

    _, servers = get_server_by_id(server_id)
    servers.append(new_server)
    save_servers(servers)

    return jsonify({"status": "success", "message": "Server created successfully", "server": new_server}), 201

@app.route('/api/start/<server_id>', methods=['POST'])
def api_start_server(server_id):
    server, servers = get_server_by_id(server_id)
    if not server:
        return jsonify({"status": "error", "message": "Server not found"}), 404

    server_dir = get_server_directory(server_id)
    if not server_dir or not os.path.exists(server_dir):
        return jsonify({"status": "error", "message": "Server directory missing"}), 404

    with process_lock:
        if server_id in active_processes:
            existing = active_processes[server_id]['process']
            if existing.poll() is None:
                return jsonify({"status": "error", "message": "Server is already running"}), 400
            else:
                try:
                    active_processes[server_id]['log_file'].close()
                except Exception:
                    pass
                del active_processes[server_id]

        startup_file = server.get('startup_file', 'main.py')
        startup_file_path = os.path.join(server_dir, startup_file)
        if not os.path.exists(startup_file_path):
            return jsonify({"status": "error", "message": f"Startup file '{startup_file}' not found"}), 400

        log_path = os.path.join(server_dir, 'output.log')
        log_file = open(log_path, 'a', encoding='utf-8', buffering=1)
        log_file.write(f"\n--- [Process Launch: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ---\n")
        log_file.flush()

        # Optional requirements check/install
        req_file = server.get('requirements_file', 'requirements.txt')
        req_file_path = os.path.join(server_dir, req_file)
        if os.path.exists(req_file_path) and os.path.getsize(req_file_path) > 0:
            try:
                subprocess.run(
                    [sys.executable, "-m", "pip", "install", "-r", req_file_path],
                    cwd=server_dir,
                    stdout=log_file,
                    stderr=log_file,
                    timeout=30
                )
            except Exception as e:
                log_file.write(f"[Startup Notice] pip install skipped/failed: {str(e)}\n")

        # Launch Python process unbuffered (-u)
        cmd = [sys.executable, "-u", startup_file]
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=server_dir,
                stdin=subprocess.PIPE,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1
            )
            active_processes[server_id] = {
                "process": proc,
                "log_file": log_file
            }
            server['status'] = 'running'
            server['pid'] = proc.pid
            server['started_at'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            save_servers(servers)
        except Exception as ex:
            log_file.write(f"[Process Error] Failed to execute: {str(ex)}\n")
            log_file.close()
            return jsonify({"status": "error", "message": f"Failed to start process: {str(ex)}"}), 500

    return jsonify({"status": "success", "message": "Server started successfully", "pid": proc.pid})

@app.route('/api/stop/<server_id>', methods=['POST'])
def api_stop_server(server_id):
    server, servers = get_server_by_id(server_id)
    if not server:
        return jsonify({"status": "error", "message": "Server not found"}), 404

    with process_lock:
        proc_entry = active_processes.get(server_id)
        if proc_entry:
            proc = proc_entry['process']
            log_file = proc_entry['log_file']
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2)
            except Exception:
                pass

            try:
                log_file.write(f"\n--- [Process Stopped: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ---\n")
                log_file.close()
            except Exception:
                pass

            del active_processes[server_id]

        server['status'] = 'stopped'
        server['pid'] = None
        save_servers(servers)

    return jsonify({"status": "success", "message": "Server stopped successfully"})

@app.route('/api/restart/<server_id>', methods=['POST'])
def api_restart_server(server_id):
    stop_resp = api_stop_server(server_id)
    if stop_resp[1] != 200 if isinstance(stop_resp, tuple) else False:
        return stop_resp
    return api_start_server(server_id)

@app.route('/api/server/<server_id>', methods=['DELETE'])
def api_delete_server(server_id):
    server, servers = get_server_by_id(server_id)
    if not server:
        return jsonify({"status": "error", "message": "Server not found"}), 404

    # Stop if currently running
    with process_lock:
        if server_id in active_processes:
            proc_entry = active_processes[server_id]
            try:
                proc_entry['process'].kill()
                proc_entry['log_file'].close()
            except Exception:
                pass
            del active_processes[server_id]

    server_dir = get_server_directory(server_id)
    if server_dir and os.path.exists(server_dir):
        shutil.rmtree(server_dir, ignore_errors=True)

    servers = [s for s in servers if s.get('id') != server_id]
    save_servers(servers)

    return jsonify({"status": "success", "message": f"Server {server_id} deleted successfully"})

# -------------------------------------------------------------------------
# Console & Logs APIs
# -------------------------------------------------------------------------

@app.route('/api/logs/<server_id>', methods=['GET'])
def api_get_logs(server_id):
    server_dir = get_server_directory(server_id)
    if not server_dir or not os.path.exists(server_dir):
        return jsonify({"status": "error", "message": "Server folder not found"}), 404

    log_path = os.path.join(server_dir, 'output.log')
    logs = ""
    if os.path.exists(log_path):
        try:
            with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
                logs = f.read()
        except Exception as e:
            logs = f"Error reading logs: {str(e)}"
    else:
        logs = "[No log file found]"

    return jsonify({"status": "success", "logs": logs})

@app.route('/api/clear_logs/<server_id>', methods=['POST'])
def api_clear_logs(server_id):
    server_dir = get_server_directory(server_id)
    if not server_dir or not os.path.exists(server_dir):
        return jsonify({"status": "error", "message": "Server folder not found"}), 404

    log_path = os.path.join(server_dir, 'output.log')
    try:
        with open(log_path, 'w', encoding='utf-8') as f:
            f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Logs cleared.\n")
        return jsonify({"status": "success", "message": "Logs cleared successfully"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/command', methods=['POST'])
@app.route('/api/command/<server_id>', methods=['POST'])
def api_send_command(server_id=None):
    data = request.get_json() or {}
    target_id = server_id or data.get('server_id')
    cmd = data.get('command', '')

    if not target_id:
        return jsonify({"status": "error", "message": "server_id is required"}), 400
    if not cmd:
        return jsonify({"status": "error", "message": "command is required"}), 400

    server_dir = get_server_directory(target_id)
    log_path = os.path.join(server_dir, 'output.log') if server_dir else None

    with process_lock:
        proc_entry = active_processes.get(target_id)
        if not proc_entry or proc_entry['process'].poll() is not None:
            return jsonify({"status": "error", "message": "Server is not running. Cannot send command."}), 400

        proc = proc_entry['process']
        try:
            # Append interactive command to console log
            if log_path and os.path.exists(log_path):
                with open(log_path, 'a', encoding='utf-8') as lf:
                    lf.write(f"> {cmd}\n")

            proc.stdin.write(cmd + "\n")
            proc.stdin.flush()
            return jsonify({"status": "success", "message": "Command sent"})
        except Exception as e:
            return jsonify({"status": "error", "message": f"Failed to send command: {str(e)}"}), 500

# -------------------------------------------------------------------------
# File Manager APIs
# -------------------------------------------------------------------------

@app.route('/api/files/<server_id>', methods=['GET'])
def api_list_files(server_id):
    server_dir = get_server_directory(server_id)
    if not server_dir or not os.path.exists(server_dir):
        return jsonify({"status": "error", "message": "Server folder not found"}), 404

    req_path = request.args.get('path', '').strip()
    target_dir = resolve_safe_path(server_dir, req_path)
    if not target_dir or not os.path.isdir(target_dir):
        return jsonify({"status": "error", "message": "Invalid directory path"}), 400

    items = []
    try:
        with os.scandir(target_dir) as entries:
            for entry in entries:
                stat = entry.stat()
                mod_time = datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M')
                is_dir = entry.is_dir()
                items.append({
                    "name": entry.name,
                    "type": "folder" if is_dir else ("zip" if entry.name.lower().endswith('.zip') else "file"),
                    "size": "-" if is_dir else format_file_size(stat.st_size),
                    "bytes": 0 if is_dir else stat.st_size,
                    "modified": mod_time,
                    "is_dir": is_dir
                })
        # Sort folders first, then files alphabetically
        items.sort(key=lambda x: (not x['is_dir'], x['name'].lower()))
        rel_path = os.path.relpath(target_dir, server_dir).replace('\\', '/')
        if rel_path == '.':
            rel_path = ''
        return jsonify({"status": "success", "current_path": rel_path, "items": items})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/file/<server_id>', methods=['GET', 'POST', 'DELETE'])
def api_file_operations(server_id):
    server_dir = get_server_directory(server_id)
    if not server_dir or not os.path.exists(server_dir):
        return jsonify({"status": "error", "message": "Server folder not found"}), 404

    if request.method == 'GET':
        path_param = request.args.get('path', '').strip()
        target_path = resolve_safe_path(server_dir, path_param)
        if not target_path or not os.path.exists(target_path):
            return jsonify({"status": "error", "message": "File not found"}), 404
        if os.path.isdir(target_path):
            return jsonify({"status": "error", "message": "Specified path is a directory"}), 400

        # If download parameter specified
        if request.args.get('download') == 'true':
            return send_file(target_path, as_attachment=True, download_name=os.path.basename(target_path))

        try:
            with open(target_path, 'r', encoding='utf-8', errors='replace') as f:
                content = f.read()
            return jsonify({"status": "success", "content": content, "filename": os.path.basename(target_path)})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

    elif request.method == 'POST':
        data = request.get_json() or {}
        file_path_rel = data.get('path', '').strip()
        content = data.get('content', '')
        if not file_path_rel:
            return jsonify({"status": "error", "message": "File path is required"}), 400

        target_path = resolve_safe_path(server_dir, file_path_rel)
        if not target_path:
            return jsonify({"status": "error", "message": "Invalid file path (directory traversal blocked)"}), 400

        try:
            os.makedirs(os.path.dirname(target_path), exist_ok=True)
            with open(target_path, 'w', encoding='utf-8') as f:
                f.write(content)
            return jsonify({"status": "success", "message": "File saved successfully"})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

    elif request.method == 'DELETE':
        data = request.get_json() or {}
        file_path_rel = data.get('path', '').strip()
        if not file_path_rel:
            return jsonify({"status": "error", "message": "Path is required"}), 400

        target_path = resolve_safe_path(server_dir, file_path_rel)
        if not target_path or not os.path.exists(target_path):
            return jsonify({"status": "error", "message": "Target not found"}), 404

        try:
            if os.path.isdir(target_path):
                shutil.rmtree(target_path)
            else:
                os.remove(target_path)
            return jsonify({"status": "success", "message": "Deleted successfully"})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/upload/<server_id>', methods=['POST'])
def api_upload_file(server_id):
    server_dir = get_server_directory(server_id)
    if not server_dir or not os.path.exists(server_dir):
        return jsonify({"status": "error", "message": "Server folder not found"}), 404

    if 'file' not in request.files:
        return jsonify({"status": "error", "message": "No file uploaded"}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({"status": "error", "message": "No file selected"}), 400

    target_sub = request.form.get('path', '').strip()
    target_dir = resolve_safe_path(server_dir, target_sub)
    if not target_dir or not os.path.isdir(target_dir):
        target_dir = server_dir

    filename = secure_filename(file.filename)
    if not filename:
        filename = f"upload_{uuid.uuid4().hex[:6]}.dat"

    save_path = os.path.join(target_dir, filename)
    try:
        file.save(save_path)
        return jsonify({"status": "success", "message": f"File '{filename}' uploaded successfully"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/create_folder/<server_id>', methods=['POST'])
def api_create_folder(server_id):
    server_dir = get_server_directory(server_id)
    if not server_dir or not os.path.exists(server_dir):
        return jsonify({"status": "error", "message": "Server folder not found"}), 404

    data = request.get_json() or {}
    folder_name = secure_filename(data.get('folder_name', '').strip())
    parent_path = data.get('path', '').strip()

    if not folder_name:
        return jsonify({"status": "error", "message": "Valid folder name required"}), 400

    target_parent = resolve_safe_path(server_dir, parent_path)
    if not target_parent or not os.path.isdir(target_parent):
        return jsonify({"status": "error", "message": "Invalid directory"}), 400

    new_folder_path = os.path.join(target_parent, folder_name)
    try:
        os.makedirs(new_folder_path, exist_ok=False)
        return jsonify({"status": "success", "message": f"Folder '{folder_name}' created successfully"})
    except FileExistsError:
        return jsonify({"status": "error", "message": "Folder already exists"}), 400
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/rename/<server_id>', methods=['POST'])
def api_rename(server_id):
    server_dir = get_server_directory(server_id)
    if not server_dir or not os.path.exists(server_dir):
        return jsonify({"status": "error", "message": "Server folder not found"}), 404

    data = request.get_json() or {}
    old_path_rel = data.get('old_path', '').strip()
    new_name = secure_filename(data.get('new_name', '').strip())

    if not old_path_rel or not new_name:
        return jsonify({"status": "error", "message": "old_path and valid new_name are required"}), 400

    old_target = resolve_safe_path(server_dir, old_path_rel)
    if not old_target or not os.path.exists(old_target):
        return jsonify({"status": "error", "message": "Target item not found"}), 404

    parent_dir = os.path.dirname(old_target)
    new_target = os.path.join(parent_dir, new_name)

    if os.path.exists(new_target):
        return jsonify({"status": "error", "message": "An item with that name already exists"}), 400

    try:
        os.rename(old_target, new_target)
        return jsonify({"status": "success", "message": "Item renamed successfully"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/extract/<server_id>', methods=['POST'])
def api_extract_zip(server_id):
    server_dir = get_server_directory(server_id)
    if not server_dir or not os.path.exists(server_dir):
        return jsonify({"status": "error", "message": "Server folder not found"}), 404

    data = request.get_json() or {}
    zip_path_rel = data.get('path', '').strip()
    zip_target = resolve_safe_path(server_dir, zip_path_rel)

    if not zip_target or not os.path.isfile(zip_target) or not zipfile.is_zipfile(zip_target):
        return jsonify({"status": "error", "message": "Valid ZIP archive required"}), 400

    extract_to = os.path.dirname(zip_target)

    try:
        with zipfile.ZipFile(zip_target, 'r') as zf:
            for member in zf.namelist():
                # Defend against Zip Slip vulnerability
                member_path = os.path.abspath(os.path.join(extract_to, member))
                if not member_path.startswith(extract_to + os.sep) and member_path != extract_to:
                    raise Exception("Attempted Zip Slip directory traversal")
            zf.extractall(path=extract_to)
        return jsonify({"status": "success", "message": "ZIP archive extracted successfully"})
    except Exception as e:
        return jsonify({"status": "error", "message": f"Extraction failed: {str(e)}"}), 500

# -------------------------------------------------------------------------
# Server Settings APIs
# -------------------------------------------------------------------------

@app.route('/api/get_startup/<server_id>', methods=['GET'])
def api_get_startup(server_id):
    server, _ = get_server_by_id(server_id)
    if not server:
        return jsonify({"status": "error", "message": "Server not found"}), 404
    return jsonify({
        "status": "success",
        "startup_file": server.get('startup_file', 'main.py'),
        "requirements_file": server.get('requirements_file', 'requirements.txt')
    })

@app.route('/api/set_startup/<server_id>', methods=['POST'])
@app.route('/api/server/<server_id>/settings', methods=['POST'])
def api_save_settings(server_id):
    server, servers = get_server_by_id(server_id)
    if not server:
        return jsonify({"status": "error", "message": "Server not found"}), 404

    data = request.get_json() or {}
    new_name = data.get('name')
    startup_file = data.get('startup_file')
    requirements_file = data.get('requirements_file')

    if new_name is not None and str(new_name).strip():
        server['name'] = str(new_name).strip()
    if startup_file is not None and str(startup_file).strip():
        server['startup_file'] = secure_filename(str(startup_file).strip())
    if requirements_file is not None and str(requirements_file).strip():
        server['requirements_file'] = secure_filename(str(requirements_file).strip())

    save_servers(servers)
    return jsonify({"status": "success", "message": "Server settings updated successfully", "server": server})

# -------------------------------------------------------------------------
# Entrypoint
# -------------------------------------------------------------------------

if __name__ == '__main__':
    ensure_environment()
    print(" * ServerPanel running on http://127.0.0.1:5000")
    app.run(host='0.0.0.0', port=5000, debug=True)
