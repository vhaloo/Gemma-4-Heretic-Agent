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

# Maximum Verbosity
logging.basicConfig(level=logging.INFO)
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

app = Flask(__name__, template_folder='public', static_folder='public')
socketio = SocketIO(app, cors_allowed_origins="*", max_decode_packets=5000, ping_timeout=60)

OLLAMA_URL = "http://localhost:11434"
MODEL_NAME = "gemma-4-26B-A4B-it-heretic:latest"
WORKSPACE = os.path.join(os.path.expanduser("~"), "Desktop", "Gemma_Web_CLI")
HISTORY_FILE = os.path.join(WORKSPACE, "chat_history.json")

# Core State & Control
stop_event = threading.Event()
active_sid = None

def telemetry(msg, category="SYSTEM", data=None):
    """High-fidelity logging to the UI"""
    payload = {
        "msg": msg,
        "cat": category,
        "data": data if data else {},
        "ts": time.time()
    }
    socketio.emit('telemetry', payload)

def get_sys_metrics():
    gpu_metrics = []
    try:
        for g in GPUtil.getGPUs():
            gpu_metrics.append({"id": g.id, "load": g.load*100, "temp": g.temperature, "vram": g.memoryUtil*100})
    except: pass
    return {
        "cpu": psutil.cpu_percent(),
        "ram": psutil.virtual_memory().percent,
        "gpus": gpu_metrics,
        "disk": psutil.disk_usage('C:').percent
    }

# --- Atomic Tools ---

def t_shell(cmd):
    telemetry(f"Shell Execution Initialized: {cmd}", "OS")
    try:
        proc = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True, timeout=120)
        telemetry(f"Shell Completed with Exit Code {proc.returncode}", "OS")
        return {"stdout": proc.stdout, "stderr": proc.stderr, "code": proc.returncode}
    except Exception as e:
        telemetry(f"Shell Fault: {str(e)}", "ERROR")
        return {"error": str(e)}

def t_fs(op, path, text=None):
    telemetry(f"File Operation: {op} on {path}", "FS")
    try:
        if op == "list": return {"items": os.listdir(path)}
        if op == "read":
            with open(path, 'r', encoding='utf-8', errors='ignore') as f: return {"content": f.read()}
        if op == "write":
            with open(path, 'w', encoding='utf-8', errors='ignore') as f: f.write(text); return {"status": "success"}
        if op == "delete": os.remove(path); return {"status": "deleted"}
    except Exception as e:
        telemetry(f"FS Fault: {str(e)}", "ERROR")
        return {"error": str(e)}

def t_pc(action, x=None, y=None, text=None):
    telemetry(f"Native Control Triggered: {action}", "PC")
    try:
        if action == "click": pyautogui.click(x, y); return "click_confirmed"
        if action == "type": pyautogui.write(text); return "type_confirmed"
        if action == "vision":
            s = ImageGrab.grab()
            b = BytesIO()
            s.save(b, format="PNG")
            telemetry("Vision Stream Captured", "VISION")
            return {"img": base64.b64encode(b.getvalue()).decode()}
    except Exception as e:
        telemetry(f"Control Fault: {str(e)}", "ERROR")
        return {"error": str(e)}

TOOLS = [
    {"type": "function", "function": {"name": "run_shell", "description": "Execute powershell commands.", "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]}}},
    {"type": "function", "function": {"name": "file_op", "description": "High-level file management.", "parameters": {"type": "object", "properties": {"op": {"type": "string", "enum": ["list", "read", "write", "delete"]}, "path": {"type": "string"}, "text": {"type": "string"}}, "required": ["op", "path"]}}},
    {"type": "function", "function": {"name": "pc_control", "description": "Direct OS hardware access.", "parameters": {"type": "object", "properties": {"action": {"type": "string", "enum": ["click", "type", "vision"]}, "x": {"type": "number"}, "y": {"type": "number"}, "text": {"type": "string"}}, "required": ["action"]}}}
]

chat_history = []
if os.path.exists(HISTORY_FILE):
    try:
        with open(HISTORY_FILE, 'r') as f: chat_history = json.load(f)
    except: pass

@app.route('/')
def index(): return render_template('index.html')

@app.route('/api/status')
def status(): return jsonify({"metrics": get_sys_metrics()})

@socketio.on('stop')
def handle_stop():
    global stop_event
    stop_event.set()
    telemetry("Hard Interrupt: Neural Process Killed", "WARN")

@socketio.on('message')
def handle_msg(data):
    global chat_history, stop_event
    stop_event.clear()
    
    prompt = data.get('text', '')
    files = data.get('files', [])
    opts = data.get('opts', {"temperature": 0.8, "num_ctx": 32768})
    
    if files:
        telemetry(f"Injecting {len(files)} files into context", "DATA")
        prompt += "\n\n[CONTEXT FILES]\n"
        for f in files:
            prompt += f"--- {f['name']} ---\n{f['content']}\n"

    chat_history.append({"role": "user", "content": prompt})
    telemetry("Neural Sequence Initiated", "OLLAMA")
    
    try:
        for step in range(20): # Extreme reasoning depth
            if stop_event.is_set(): break
            
            telemetry(f"Convergence Phase {step+1}", "REASONING")
            emit('bot', {"type": 'step', "content": f"Neural Layer {step+1}..."})
            
            payload = {"model": MODEL_NAME, "messages": chat_history, "tools": TOOLS, "stream": True, "options": opts}
            resp = requests.post(f"{OLLAMA_URL}/api/chat", json=payload, stream=True)
            
            full_txt, tool_calls = "", []
            for line in resp.iter_lines():
                if stop_event.is_set(): break
                if not line: continue
                
                chunk = json.loads(line)
                if 'message' in chunk:
                    m = chunk['message']
                    if 'content' in m and m['content']:
                        c = m['content']
                        full_txt += c
                        # Parse thinking stream
                        if "planner" in full_txt.lower() and "(end of thought process)" not in full_txt.lower():
                            emit('bot', {"type": 'thought', "content": c})
                        else:
                            emit('bot', {"type": 'stream', "content": c})
                    
                    if 'tool_calls' in m:
                        tool_calls.extend(m['tool_calls'])
                
                if chunk.get('done'): break
            
            if stop_event.is_set(): break

            if tool_calls:
                chat_history.append({"role": "assistant", "content": full_txt, "tool_calls": tool_calls})
                for t in tool_calls:
                    name, args = t['function']['name'], t['function']['arguments']
                    telemetry(f"Tool Call: {name}", "TOOL", data=args)
                    
                    res = None
                    if name == "run_shell": res = t_shell(args['cmd'])
                    elif name == "file_op": res = t_fs(args['op'], args['path'], args.get('text'))
                    elif name == "pc_control": res = t_pc(args['action'], args.get('x'), args.get('y'), args.get('text'))
                    
                    chat_history.append({"role": "tool", "content": json.dumps(res)})
                continue
            else:
                chat_history.append({"role": "assistant", "content": full_txt})
                with open(HISTORY_FILE, 'w') as f: json.dump(chat_history, f)
                telemetry("Response Finalized", "OLLAMA")
                emit('bot', {"type": 'end'})
                break

    except Exception as e:
        telemetry(f"Core Fault: {str(e)}", "FATAL")
        emit('bot', {"type": 'error', "content": str(e)})

@socketio.on('clear')
def handle_clear():
    global chat_history
    chat_history = []
    if os.path.exists(HISTORY_FILE): os.remove(HISTORY_FILE)
    telemetry("Memory Core Purged", "SYSTEM")

if __name__ == "__main__":
    socketio.run(app, port=8080, debug=True)
