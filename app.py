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

# Silent logging
logging.getLogger('werkzeug').setLevel(logging.ERROR)

app = Flask(__name__, template_folder='public', static_folder='public')
socketio = SocketIO(app, cors_allowed_origins="*", max_decode_packets=1000)

OLLAMA_URL = "http://localhost:11434"
MODEL_NAME = "gemma-4-26B-A4B-it-heretic:latest"
WORKSPACE = os.path.join(os.path.expanduser("~"), "Desktop", "Gemma_Web_CLI")
HISTORY_FILE = os.path.join(WORKSPACE, "chat_history.json")
os.makedirs(WORKSPACE, exist_ok=True)

# State
stop_flags = {}

def trace(msg, level="INFO"):
    socketio.emit('trace', {"msg": msg, "lvl": level, "ts": time.time()})

def get_sys():
    gpu_data = []
    try:
        gpus = GPUtil.getGPUs()
        for g in gpus:
            gpu_data.append({"load": g.load*100, "temp": g.temperature})
    except: pass
    return {"cpu": psutil.cpu_percent(), "ram": psutil.virtual_memory().percent, "gpus": gpu_data}

# --- Tools ---

def t_shell(cmd):
    trace(f"Terminal Command: {cmd}", "EXEC")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True, timeout=120)
        return {"stdout": r.stdout, "stderr": r.stderr, "code": r.returncode}
    except Exception as e: return {"error": str(e)}

def t_fs(op, path, text=None):
    trace(f"Disk Access: {op} on {path}", "FS")
    try:
        if op == "list": return {"items": os.listdir(path)}
        if op == "read":
            with open(path, 'r', encoding='utf-8', errors='ignore') as f: return {"content": f.read()}
        if op == "write":
            with open(path, 'w', encoding='utf-8') as f: f.write(text); return {"status": "ok"}
        if op == "delete": os.remove(path); return {"status": "deleted"}
    except Exception as e: return {"error": str(e)}

def t_pc(action, x=None, y=None, text=None):
    trace(f"OS Control: {action}", "PC")
    try:
        if action == "click": pyautogui.click(x, y); return "click_ok"
        if action == "type": pyautogui.write(text); return "type_ok"
        if action == "vision":
            s = ImageGrab.grab()
            b = BytesIO()
            s.save(b, format="PNG")
            return {"img": base64.b64encode(b.getvalue()).decode()}
    except Exception as e: return {"error": str(e)}

TOOLS = [
    {"type": "function", "function": {"name": "run_shell", "description": "Run shell commands.", "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]}}},
    {"type": "function", "function": {"name": "file_op", "description": "Manage files.", "parameters": {"type": "object", "properties": {"op": {"type": "string", "enum": ["list", "read", "write", "delete"]}, "path": {"type": "string"}, "text": {"type": "string"}}, "required": ["op", "path"]}}},
    {"type": "function", "function": {"name": "pc_control", "description": "Keyboard/Mouse/Vision.", "parameters": {"type": "object", "properties": {"action": {"type": "string", "enum": ["click", "type", "vision"]}, "x": {"type": "number"}, "y": {"type": "number"}, "text": {"type": "string"}}, "required": ["action"]}}}
]

chat_history = []
if os.path.exists(HISTORY_FILE):
    try:
        with open(HISTORY_FILE, 'r') as f: chat_history = json.load(f)
    except: pass

@app.route('/')
def index(): return render_template('index.html')

@app.route('/api/status')
def status(): return jsonify({"stats": get_sys(), "ready": True})

@socketio.on('stop')
def handle_stop(): stop_flags[request.sid] = True

@socketio.on('message')
def handle_msg(data):
    global chat_history
    sid = request.sid
    stop_flags[sid] = False
    
    prompt = data.get('text', '')
    imgs = data.get('imgs', [])
    files = data.get('files', []) # [{name, content, type}]
    opts = data.get('opts', {"temperature": 0.7})
    
    # Pre-process files into prompt
    if files:
        prompt += "\n\nAttached Files:\n"
        for f in files:
            prompt += f"--- {f['name']} ---\n{f['content']}\n"

    msg = {"role": "user", "content": prompt}
    if imgs: msg["images"] = imgs
    chat_history.append(msg)
    
    try:
        for step in range(15):
            if stop_flags.get(sid): break
            emit('bot', {"type": 'step', "content": f"Neural Link Stage {step+1}..."})
            trace(f"Step {step+1}: Ollama Request", "OLLAMA")
            
            payload = {"model": MODEL_NAME, "messages": chat_history, "tools": TOOLS, "stream": True, "options": opts}
            resp = requests.post(f"{OLLAMA_URL}/api/chat", json=payload, stream=True)
            
            full_txt, tool_calls = "", []
            for line in resp.iter_lines():
                if stop_flags.get(sid): break
                if not line: continue
                chunk = json.loads(line)
                if 'message' in chunk:
                    m = chunk['message']
                    if 'content' in m and m['content']:
                        c = m['content']
                        full_txt += c
                        if "planner" in full_txt.lower() and "(end of thought process)" not in full_txt.lower():
                            emit('bot', {"type": 'thought', "content": c})
                        else:
                            emit('bot', {"type": 'stream', "content": c})
                    if 'tool_calls' in m: tool_calls.extend(m['tool_calls'])
                if chunk.get('done'): break
            
            if stop_flags.get(sid): break

            if tool_calls:
                chat_history.append({"role": "assistant", "content": full_txt, "tool_calls": tool_calls})
                for t in tool_calls:
                    name, args = t['function']['name'], t['function']['arguments']
                    res = None
                    if name == "run_shell": res = t_shell(args['cmd'])
                    elif name == "file_op": res = t_fs(args['op'], args['path'], args.get('text'))
                    elif name == "pc_control": res = t_pc(args['action'], args.get('x'), args.get('y'), args.get('text'))
                    chat_history.append({"role": "tool", "content": json.dumps(res)})
                continue
            else:
                chat_history.append({"role": "assistant", "content": full_txt})
                with open(HISTORY_FILE, 'w') as f: json.dump(chat_history, f)
                emit('bot', {"type": 'end'})
                break

    except Exception as e:
        trace(f"Neural Fault: {str(e)}", "ERROR")
        emit('bot', {"type": 'error', "content": str(e)})

if __name__ == "__main__":
    socketio.run(app, port=8080, debug=True)
