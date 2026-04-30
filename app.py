import os
import json
import requests
import subprocess
import base64
import logging
import psutil
import time
import pyautogui
import GPUtil
import threading
from PIL import ImageGrab
from io import BytesIO
from flask import Flask, render_template, request, jsonify, Response
from flask_socketio import SocketIO, emit

# Silent logging for less clutter
logging.getLogger('werkzeug').setLevel(logging.ERROR)

app = Flask(__name__, template_folder='public', static_folder='public')
socketio = SocketIO(app, cors_allowed_origins="*", max_decode_packets=500)

OLLAMA_URL = "http://localhost:11434"
MODEL_NAME = "gemma-4-26B-A4B-it-heretic:latest"
WORKSPACE = os.path.join(os.path.expanduser("~"), "Desktop", "Gemma_Web_CLI")
HISTORY_FILE = os.path.join(WORKSPACE, "chat_history.json")

# Core State
stop_flags = {}

def get_sys_info():
    gpu_data = []
    try:
        gpus = GPUtil.getGPUs()
        for g in gpus:
            gpu_data.append({"load": g.load*100, "temp": g.temperature, "mem": g.memoryUtil*100})
    except: pass
    return {
        "cpu": psutil.cpu_percent(),
        "ram": psutil.virtual_memory().percent,
        "gpus": gpu_data,
        "disk": psutil.disk_usage('C:').percent
    }

# --- 10x Feature Tools ---

def tool_shell(cmd):
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True, timeout=60)
        return {"out": r.stdout, "err": r.stderr, "code": r.returncode}
    except Exception as e: return {"error": str(e)}

def tool_fs_list(path):
    try:
        items = []
        for entry in os.scandir(path):
            items.append({
                "name": entry.name,
                "is_dir": entry.is_dir(),
                "size": entry.stat().st_size if not entry.is_dir() else 0,
                "mtime": entry.stat().st_mtime
            })
        return {"items": items}
    except Exception as e: return {"error": str(e)}

def tool_fs_read(path):
    try:
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            return {"content": f.read()}
    except Exception as e: return {"error": str(e)}

def tool_fs_write(path, text):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)
        return {"status": "success"}
    except Exception as e: return {"error": str(e)}

def tool_control(action, x=None, y=None, text=None):
    try:
        if action == "click": pyautogui.click(x, y); return "clicked"
        if action == "type": pyautogui.write(text); return "typed"
        if action == "screen":
            s = ImageGrab.grab()
            b = BytesIO()
            s.save(b, format="PNG")
            return {"img": base64.b64encode(b.getvalue()).decode()}
        return "ok"
    except Exception as e: return {"error": str(e)}

TOOLS = [
    {"type": "function", "function": {"name": "run_command", "description": "Run a system command.", "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]}}},
    {"type": "function", "function": {"name": "list_dir", "description": "List files in a folder.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "read_file", "description": "Read file contents.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "write_file", "description": "Write to a file.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "text": {"type": "string"}}, "required": ["path", "text"]}}},
    {"type": "function", "function": {"name": "control_pc", "description": "Mouse/Keyboard/Screen.", "parameters": {"type": "object", "properties": {"action": {"type": "string", "enum": ["click", "type", "screen"]}, "x": {"type": "number"}, "y": {"type": "number"}, "text": {"type": "string"}}, "required": ["action"]}}}
]

# History Storage
chat_history = []
if os.path.exists(HISTORY_FILE):
    try:
        with open(HISTORY_FILE, 'r') as f: chat_history = json.load(f)
    except: pass

@app.route('/')
def index(): return render_template('index.html')

@app.route('/api/status')
def status():
    return jsonify({"stats": get_sys_info()})

@socketio.on('stop')
def handle_stop():
    stop_flags[request.sid] = True

@socketio.on('message')
def handle_msg(data):
    global chat_history
    sid = request.sid
    stop_flags[sid] = False
    
    text = data.get('text', '')
    imgs = data.get('imgs', [])
    opts = data.get('opts', {"temperature": 0.7})
    
    msg = {"role": "user", "content": text}
    if imgs: msg["images"] = imgs
    chat_history.append(msg)
    
    try:
        for step in range(20): # Ultra deep reasoning
            if stop_flags.get(sid): break
            
            emit('bot', {"type": 'step', "content": f"Reasoning Stage {step+1}..."})
            
            payload = {"model": MODEL_NAME, "messages": chat_history, "tools": TOOLS, "stream": True, "options": opts}
            resp = requests.post(f"{OLLAMA_URL}/api/chat", json=payload, stream=True)
            
            full_txt = ""
            tool_calls = []
            
            for line in resp.iter_lines():
                if stop_flags.get(sid): break
                if not line: continue
                chunk = json.loads(line)
                
                if 'message' in chunk:
                    m = chunk['message']
                    if 'content' in m and m['content']:
                        c = m['content']
                        full_txt += c
                        # Heuristic to separate thinking from answering
                        if "planner" in full_txt.lower() and "(end of thought process)" not in full_txt.lower():
                            emit('bot', {"type": 'thought', "content": c})
                        else:
                            emit('bot', {"type": 'stream', "content": c})
                    
                    if 'tool_calls' in m:
                        tool_calls.extend(m['tool_calls'])
                
                if chunk.get('done'): break
            
            if tool_calls:
                chat_history.append({"role": "assistant", "content": full_txt, "tool_calls": tool_calls})
                for t in tool_calls:
                    name = t['function']['name']
                    args = t['function']['arguments']
                    emit('bot', {"type": 'step', "content": f"Executing {name}..."})
                    
                    res = None
                    if name == "run_command": res = tool_shell(args['cmd'])
                    elif name == "list_dir": res = tool_fs_list(args['path'])
                    elif name == "read_file": res = tool_fs_read(args['path'])
                    elif name == "write_file": res = tool_fs_write(args['path'], args['text'])
                    elif name == "control_pc": res = tool_control(args['action'], args.get('x'), args.get('y'), args.get('text'))
                    
                    chat_history.append({"role": "tool", "content": json.dumps(res)})
                continue
            else:
                chat_history.append({"role": "assistant", "content": full_txt})
                with open(HISTORY_FILE, 'w') as f: json.dump(chat_history, f)
                emit('bot', {"type": 'end'})
                break

    except Exception as e:
        emit('bot', {"type": 'error', "content": str(e)})

@socketio.on('clear')
def handle_clear():
    global chat_history
    chat_history = []
    if os.path.exists(HISTORY_FILE): os.remove(HISTORY_FILE)

if __name__ == "__main__":
    socketio.run(app, port=8080, debug=True)
