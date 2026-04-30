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

# Logging setup
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

app = Flask(__name__, template_folder='public', static_folder='public')
socketio = SocketIO(app, cors_allowed_origins="*", max_decode_packets=100)

OLLAMA_URL = "http://localhost:11434"
MODEL_NAME = "gemma-4-26B-A4B-it-heretic:latest"
WORKSPACE = "C:\\Users\\Vhaloo\\Desktop\\Gemma_Web_CLI"
HISTORY_FILE = os.path.join(WORKSPACE, "chat_history.json")

# Interrupt Control
stop_flags = {}

def log_to_ui(msg, level="INFO"):
    socketio.emit('internal_log', {"msg": msg, "level": level, "ts": time.time()})

# --- Enhanced Tool Implementations ---

def tool_get_system_stats():
    gpu_stats = []
    try:
        gpus = GPUtil.getGPUs()
        for gpu in gpus:
            gpu_stats.append({
                "id": gpu.id, "name": gpu.name, "load": gpu.load * 100,
                "mem_used": gpu.memoryUsed, "mem_total": gpu.memoryTotal, "temp": gpu.temperature
            })
    except: pass
    
    net = psutil.net_io_counters()
    return {
        "cpu": psutil.cpu_percent(),
        "ram": psutil.virtual_memory().percent,
        "gpus": gpu_stats,
        "net_sent": net.bytes_sent, "net_recv": net.bytes_recv,
        "disk": psutil.disk_usage('C:').percent
    }

def tool_execute_shell(command):
    log_to_ui(f"Invoking Shell: {command}", "CMD")
    try:
        result = subprocess.run(["powershell", "-NoProfile", "-Command", command], capture_output=True, text=True, timeout=120)
        return {"stdout": result.stdout, "stderr": result.stderr, "code": result.returncode}
    except Exception as e: 
        log_to_ui(f"Shell Error: {str(e)}", "ERROR")
        return {"error": str(e)}

def tool_file_op(op, path, content=None):
    log_to_ui(f"File System: {op} on {path}", "FS")
    try:
        if op == "read":
            with open(path, 'r', encoding='utf-8') as f: return {"data": f.read()}
        elif op == "write":
            with open(path, 'w', encoding='utf-8', errors='ignore') as f: f.write(content); return {"status": "success"}
        elif op == "list":
            return {"items": os.listdir(path)}
        elif op == "info":
            s = os.stat(path)
            return {"size": s.st_size, "mtime": s.st_mtime}
    except Exception as e: 
        return {"error": str(e)}

def tool_computer_control(action, x=None, y=None, text=None):
    log_to_ui(f"Control: {action}", "OS")
    try:
        if action == "click": pyautogui.click(x, y); return "Clicked"
        if action == "type": pyautogui.write(text); return "Typed"
        if action == "press": pyautogui.press(text); return "Pressed"
        if action == "screenshot":
            s = ImageGrab.grab()
            b = BytesIO()
            s.save(b, format="PNG")
            return {"img": base64.b64encode(b.getvalue()).decode()}
        return "Done"
    except Exception as e: return {"error": str(e)}

TOOLS = [
    {"type": "function", "function": {"name": "shell", "description": "Run powershell.", "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]}}},
    {"type": "function", "function": {"name": "fs", "description": "Files.", "parameters": {"type": "object", "properties": {"op": {"type": "string", "enum": ["read", "write", "list", "info"]}, "path": {"type": "string"}, "content": {"type": "string"}}, "required": ["op", "path"]}}},
    {"type": "function", "function": {"name": "control", "description": "OS Control.", "parameters": {"type": "object", "properties": {"action": {"type": "string", "enum": ["click", "type", "press", "screenshot"]}, "x": {"type": "number"}, "y": {"type": "number"}, "text": {"type": "string"}}, "required": ["action"]}}},
    {"type": "function", "function": {"name": "web", "description": "Search.", "parameters": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}}},
    {"type": "function", "function": {"name": "stats", "description": "System usage.", "parameters": {"type": "object", "properties": {}}}}
]

# History
chat_history = []
if os.path.exists(HISTORY_FILE):
    try:
        with open(HISTORY_FILE, 'r') as f: chat_history = json.load(f)
    except: pass

@app.route('/')
def index(): return render_template('index.html')

@app.route('/api/status')
def status():
    return jsonify({"ollama": True, "stats": tool_get_system_stats()})

@socketio.on('stop_generation')
def handle_stop():
    stop_flags[request.sid] = True
    log_to_ui("Interrupt Request Received.", "SYSTEM")

@socketio.on('user_message')
def handle_message(payload):
    global chat_history
    sid = request.sid
    stop_flags[sid] = False
    
    user_text = payload.get('message', '')
    images = payload.get('images', [])
    opts = payload.get('options', {"temperature": 0.7})
    
    msg = {"role": "user", "content": user_text}
    if images: msg["images"] = images
    chat_history.append(msg)
    
    try:
        for step in range(15): # Extended reasoning depth
            if stop_flags.get(sid): break
            
            log_to_ui(f"Neural Convergence Step {step+1}...", "OLLAMA")
            emit('bot_response', {"type": 'status', "content": f"Neural Layer {step+1}: Synthesizing..."})
            
            data = {"model": MODEL_NAME, "messages": chat_history, "tools": TOOLS, "stream": True, "options": opts}
            resp = requests.post(f"{OLLAMA_URL}/api/chat", json=data, stream=True)
            
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
                        # Dynamic parsing for thinking vs responding
                        if "planner" in full_txt.lower() and "(end of thought process)" not in full_txt.lower():
                            emit('bot_response', {"type": 'thought', "content": c})
                        else:
                            emit('bot_response', {"type": 'stream', "content": c})
                    
                    if 'tool_calls' in m:
                        tool_calls.extend(m['tool_calls'])
                
                if chunk.get('done'): break
            
            if stop_flags.get(sid): 
                log_to_ui("Process Aborted by User.", "WARN")
                break

            if tool_calls:
                chat_history.append({"role": "assistant", "content": full_txt, "tool_calls": tool_calls})
                for t in tool_calls:
                    name = t['function']['name']
                    args = t['function']['arguments']
                    log_to_ui(f"Executing: {name} with args {args}", "TOOL")
                    
                    res = None
                    if name == "shell": res = tool_execute_shell(args['cmd'])
                    elif name == "fs": res = tool_file_op(args['op'], args['path'], args.get('content'))
                    elif name == "control": res = tool_computer_control(args['action'], args.get('x'), args.get('y'), args.get('text'))
                    elif name == "stats": res = tool_get_system_stats()
                    elif name == "web":
                        with DDGS() as ddgs: res = [r for r in ddgs.text(args['q'], max_results=5)]
                    
                    chat_history.append({"role": "tool", "content": json.dumps(res)})
                continue
            else:
                chat_history.append({"role": "assistant", "content": full_txt})
                with open(HISTORY_FILE, 'w') as f: json.dump(chat_history, f)
                emit('bot_response', {"type": 'stream_end'})
                break

    except Exception as e:
        log_to_ui(f"Critical System Fault: {str(e)}", "FATAL")
        emit('bot_response', {"type": 'error', "content": str(e)})

@socketio.on('clear_history')
def clear_history():
    global chat_history
    chat_history = []
    if os.path.exists(HISTORY_FILE): os.remove(HISTORY_FILE)
    log_to_ui("Memory Reset Success.", "SYSTEM")

if __name__ == "__main__":
    socketio.run(app, port=8080, debug=True)
