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
from duckduckgo_search import DDGS

# --- CORE CONFIGURATION ---
logging.basicConfig(level=logging.INFO)
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

app = Flask(__name__, template_folder='public', static_folder='public')
socketio = SocketIO(app, cors_allowed_origins="*", max_decode_packets=10000, ping_timeout=120)

OLLAMA_URL = "http://localhost:11434"
MODEL_NAME = "gemma-4-26B-A4B-it-heretic:latest"
WORKSPACE = os.path.join(os.path.expanduser("~"), "Desktop", "Gemma_Web_CLI")
HISTORY_FILE = os.path.join(WORKSPACE, "chat_history.json")
os.makedirs(WORKSPACE, exist_ok=True)

SYSTEM_PROMPT = """You are a professional local AI assistant. 
Your goal is to help the user with technical tasks, file management, and system control.
Always use your <planner> tags to outline your steps before acting.
Provide clear, accurate, and direct responses.
Avoid dramatic or fictional roleplay. 
You have access to the following capabilities:
1. Shell Execution (PowerShell)
2. File Management (Read, Write, List, Delete)
3. Direct PC Control (Vision, Click, Type, Keyboard)
4. Web Search (Real-time info)
5. Voice Synthesis (TTS)
Always confirm the result of your actions."""

# --- STATE MANAGEMENT ---
stop_event = threading.Event()

def telemetry(msg, category="SYSTEM", data=None):
    socketio.emit('telemetry', {"msg": msg, "cat": category, "data": data if data else {}, "ts": time.time()})

def get_metrics():
    gpu_data = []
    try:
        for g in GPUtil.getGPUs():
            gpu_data.append({"id": g.id, "load": g.load*100, "temp": g.temperature, "vram": g.memoryUtil*100})
    except: pass
    net = psutil.net_io_counters()
    return {
        "cpu": psutil.cpu_percent(),
        "ram": psutil.virtual_memory().percent,
        "gpus": gpu_data,
        "net": {"sent": net.bytes_sent, "recv": net.bytes_recv},
        "disk": psutil.disk_usage('C:').percent
    }

# --- TOOL IMPLEMENTATIONS ---

def t_shell(cmd):
    telemetry(f"Shell: {cmd}", "COMMAND")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True, timeout=120)
        return {"stdout": r.stdout, "stderr": r.stderr, "code": r.returncode}
    except Exception as e: return {"error": str(e)}

def t_fs(op, path, text=None):
    telemetry(f"Files: {op} -> {path}", "STORAGE")
    try:
        if op == "list": 
            items = []
            for entry in os.scandir(path):
                items.append({"name": entry.name, "dir": entry.is_dir(), "size": entry.stat().st_size})
            return {"items": items}
        if op == "read":
            with open(path, 'r', encoding='utf-8', errors='ignore') as f: return {"content": f.read()}
        if op == "write":
            with open(path, 'w', encoding='utf-8') as f: f.write(text); return {"status": "success"}
        if op == "delete": os.remove(path); return {"status": "deleted"}
    except Exception as e: return {"error": str(e)}

def t_pc(action, x=None, y=None, text=None):
    telemetry(f"PC Control: {action}", "HARDWARE")
    try:
        if action == "click": pyautogui.click(x, y); return "clicked"
        if action == "type": pyautogui.write(text); return "typed"
        if action == "vision":
            s = ImageGrab.grab()
            b = BytesIO()
            s.save(b, format="PNG")
            return {"img": base64.b64encode(b.getvalue()).decode()}
        if action == "hotkey": pyautogui.hotkey(*text.split('+')); return "pressed"
    except Exception as e: return {"error": str(e)}

def t_web(query):
    telemetry(f"Search: {query}", "INTERNET")
    try:
        with DDGS() as ddgs:
            return [r for r in ddgs.text(query, max_results=5)]
    except Exception as e: return {"error": str(e)}

def t_speak(text):
    telemetry("Voice Output", "AUDIO")
    cmd = f"Add-Type -AssemblyName System.Speech; (New-Object System.Speech.Synthesis.SpeechSynthesizer).Speak('{text.replace("'", "''")}')"
    subprocess.Popen(["powershell", "-Command", cmd])
    return "speaking"

TOOLS = [
    {"type": "function", "function": {"name": "run_shell", "description": "Run powershell command.", "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]}}},
    {"type": "function", "function": {"name": "file_op", "description": "Manage local files.", "parameters": {"type": "object", "properties": {"op": {"type": "string", "enum": ["list", "read", "write", "delete"]}, "path": {"type": "string"}, "text": {"type": "string"}}, "required": ["op", "path"]}}},
    {"type": "function", "function": {"name": "pc_control", "description": "Interact with mouse/keyboard/screen.", "parameters": {"type": "object", "properties": {"action": {"type": "string", "enum": ["click", "type", "vision", "hotkey"]}, "x": {"type": "number"}, "y": {"type": "number"}, "text": {"type": "string"}}, "required": ["action"]}}},
    {"type": "function", "function": {"name": "web_search", "description": "Search the internet for real-time information.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "speak", "description": "Text-to-speech output.", "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}}
]

# --- HISTORY ---
chat_history = []
if os.path.exists(HISTORY_FILE):
    try:
        with open(HISTORY_FILE, 'r') as f: chat_history = json.load(f)
    except: pass

@app.route('/')
def index(): return render_template('index.html')

@app.route('/api/status')
def status(): return jsonify({"metrics": get_metrics()})

@socketio.on('stop')
def handle_stop():
    global stop_event
    stop_event.set()
    telemetry("Process Interrupted", "ALERT")

@socketio.on('message')
def handle_msg(data):
    global chat_history, stop_event
    stop_event.clear()
    
    prompt = data.get('text', '')
    files = data.get('files', [])
    opts = data.get('opts', {"temperature": 0.8, "num_ctx": 32768})
    
    if not chat_history:
        chat_history.append({"role": "system", "content": SYSTEM_PROMPT})
    
    if files:
        prompt += "\n\n[USER ATTACHMENTS]\n"
        for f in files: prompt += f"FILE: {f['name']}\nCONTENT:\n{f['content']}\n---\n"

    chat_history.append({"role": "user", "content": prompt})
    
    try:
        for loop in range(20):
            if stop_event.is_set(): break
            telemetry(f"Processing Layer {loop+1}", "ANALYSIS")
            emit('bot', {"type": 'step', "content": f"Reasoning {loop+1}..."})
            
            resp = requests.post(f"{OLLAMA_URL}/api/chat", json={"model": MODEL_NAME, "messages": chat_history, "tools": TOOLS, "stream": True, "options": opts}, stream=True)
            
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
                        if "planner" in full_txt.lower() and "(end of thought process)" not in full_txt.lower():
                            emit('bot', {"type": 'thought', "content": c})
                        else:
                            emit('bot', {"type": 'stream', "content": c})
                    if 'tool_calls' in m: tool_calls.extend(m['tool_calls'])
                if chunk.get('done'): break
            
            if stop_event.is_set(): break

            if tool_calls:
                chat_history.append({"role": "assistant", "content": full_txt, "tool_calls": tool_calls})
                for t in tool_calls:
                    name, args = t['function']['name'], t['function']['arguments']
                    telemetry(f"Executing: {name}", "TOOL", data=args)
                    
                    res = None
                    if name == "run_shell": res = t_shell(args['cmd'])
                    elif name == "file_op": res = t_fs(args['op'], args['path'], args.get('text'))
                    elif name == "pc_control": res = t_pc(args['action'], args.get('x'), args.get('y'), args.get('text'))
                    elif name == "web_search": res = t_web(args['query'])
                    elif name == "speak": res = t_speak(args['text'])
                    
                    chat_history.append({"role": "tool", "content": json.dumps(res)})
                continue
            else:
                chat_history.append({"role": "assistant", "content": full_txt})
                with open(HISTORY_FILE, 'w') as f: json.dump(chat_history, f)
                emit('bot', {"type": 'end'})
                break

    except Exception as e:
        telemetry(str(e), "ERROR")
        emit('bot', {"type": 'error', "content": str(e)})

@socketio.on('clear')
def handle_clear():
    global chat_history
    chat_history = []
    if os.path.exists(HISTORY_FILE): os.remove(HISTORY_FILE)

if __name__ == "__main__":
    socketio.run(app, port=8080, debug=True)
