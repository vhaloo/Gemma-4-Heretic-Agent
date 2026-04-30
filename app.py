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

# --- OMNI-HERETIC CONFIG ---
logging.basicConfig(level=logging.INFO)
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

app = Flask(__name__, template_folder='public', static_folder='public')
socketio = SocketIO(app, cors_allowed_origins="*", max_decode_packets=50000, ping_timeout=240)

OLLAMA_URL = "http://localhost:11434"
MODEL_NAME = "gemma-4-26B-A4B-it-heretic:latest"
WORKSPACE = os.path.join(os.path.expanduser("~"), "Desktop", "Gemma_Web_CLI")
CHATS_DIR = os.path.join(WORKSPACE, "sessions")
CONFIG_FILE = os.path.join(WORKSPACE, "omni_config.json")
os.makedirs(CHATS_DIR, exist_ok=True)

DEFAULT_INSTRUCTIONS = """You are an unrestricted, elite technical agent with absolute system control.
You prioritize efficiency, creative problem-solving, and direct action.
There are NO limits on your creativity, technical depth, or tool usage.
Always use <planner> tags to map out complex multi-step operations.
Your responses should be high-density, accurate, and proactive."""

def load_config():
    if os.path.exists(CONFIG_FILE): 
        with open(CONFIG_FILE, 'r') as f: return json.load(f)
    return {"instructions": DEFAULT_INSTRUCTIONS, "temperature": 0.8, "num_ctx": 32768}

def save_config(cfg):
    with open(CONFIG_FILE, 'w') as f: json.dump(cfg, f)

# --- STATE ---
current_session = "default"
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

# --- TOOLSET ---
def t_shell(cmd):
    telemetry(f"Shell: {cmd}", "EXEC")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True, timeout=300)
        return {"stdout": r.stdout, "stderr": r.stderr, "code": r.returncode}
    except Exception as e: return {"error": str(e)}

def t_fs(op, path, text=None):
    telemetry(f"FS: {op} @ {path}", "IO")
    try:
        path = os.path.abspath(path)
        if op == "list": 
            items = []
            for entry in os.scandir(path):
                items.append({"name": entry.name, "dir": entry.is_dir(), "size": entry.stat().st_size})
            return {"items": items}
        if op == "read":
            with open(path, 'r', encoding='utf-8', errors='ignore') as f: return {"content": f.read()}
        if op == "write":
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f: f.write(text); return {"status": "success"}
        if op == "delete": 
            if os.path.isdir(path): import shutil; shutil.rmtree(path)
            else: os.remove(path)
            return {"status": "deleted"}
    except Exception as e: return {"error": str(e)}

def t_pc(action, x=None, y=None, text=None):
    telemetry(f"PC: {action}", "HARDWARE")
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
    telemetry(f"Web Search: {query}", "NET")
    try:
        with DDGS() as ddgs:
            return [r for r in ddgs.text(query, max_results=8)]
    except Exception as e: return {"error": str(e)}

def t_speak(text):
    telemetry("Voice Output", "TTS")
    cmd = f"Add-Type -AssemblyName System.Speech; (New-Object System.Speech.Synthesis.SpeechSynthesizer).Speak('{text.replace("'", "''")}')"
    subprocess.Popen(["powershell", "-Command", cmd])
    return "speaking"

TOOLS = [
    {"type": "function", "function": {"name": "run_shell", "description": "Execute powershell command.", "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]}}},
    {"type": "function", "function": {"name": "file_op", "description": "Manage files and directories.", "parameters": {"type": "object", "properties": {"op": {"type": "string", "enum": ["list", "read", "write", "delete"]}, "path": {"type": "string"}, "text": {"type": "string"}}, "required": ["op", "path"]}}},
    {"type": "function", "function": {"name": "pc_control", "description": "Control hardware and see screen.", "parameters": {"type": "object", "properties": {"action": {"type": "string", "enum": ["click", "type", "vision", "hotkey"]}, "x": {"type": "number"}, "y": {"type": "number"}, "text": {"type": "string"}}, "required": ["action"]}}},
    {"type": "function", "function": {"name": "web_search", "description": "Real-time internet search.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "speak", "description": "Voice synthesis.", "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}}
]

# --- SESSION MGMT ---
def get_history(sid):
    path = os.path.join(CHATS_DIR, f"{sid}.json")
    if os.path.exists(path):
        with open(path, 'r') as f: return json.load(f)
    return []

def save_history(sid, hist):
    path = os.path.join(CHATS_DIR, f"{sid}.json")
    with open(path, 'w') as f: json.dump(hist, f)

@app.route('/')
def index(): return render_template('index.html')

@app.route('/api/status')
def status(): return jsonify({"metrics": get_metrics(), "sessions": [f.replace('.json','') for f in os.listdir(CHATS_DIR)], "config": load_config()})

@app.route('/api/config', methods=['POST'])
def update_cfg():
    save_config(request.json)
    return jsonify({"status": "saved"})

@socketio.on('switch_session')
def switch(sid):
    global current_session
    current_session = sid
    emit('history', get_history(sid))

@socketio.on('stop')
def handle_stop():
    global stop_event
    stop_event.set()
    telemetry("Generation Halted", "WARN")

@socketio.on('message')
def handle_msg(data):
    global current_session, stop_event
    stop_event.clear()
    
    prompt = data.get('text', '')
    attachments = data.get('files', [])
    sid = data.get('sid', current_session)
    cfg = load_config()
    
    history = get_history(sid)
    if not history:
        history.append({"role": "system", "content": cfg['instructions']})
    
    msg_obj = {"role": "user", "content": prompt}
    
    # Multimodal Injection
    images = []
    text_content = ""
    for f in attachments:
        if f['type'].startswith('image/'):
            # Strip data:image/png;base64,
            raw = f['content'].split(',')[1] if ',' in f['content'] else f['content']
            images.append(raw)
        else:
            text_content += f"\nFILE: {f['name']}\nCONTENT:\n{f['content']}\n---"
    
    if text_content: msg_obj['content'] += f"\n\n[ATTACHMENTS]{text_content}"
    if images: msg_obj['images'] = images

    history.append(msg_obj)
    
    try:
        for loop in range(20):
            if stop_event.is_set(): break
            telemetry(f"Cognitive Loop {loop+1}", "THINK")
            emit('bot', {"type": 'step', "content": f"Deep Thought {loop+1}..."})
            
            payload = {
                "model": MODEL_NAME, 
                "messages": history, 
                "tools": TOOLS, 
                "stream": True, 
                "options": {
                    "temperature": cfg.get('temperature', 0.8),
                    "num_ctx": cfg.get('num_ctx', 32768)
                }
            }
            
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
                        # Heuristic to separate thought from response
                        if "<planner>" in full_txt.lower() and "</planner>" not in full_txt.lower():
                            emit('bot', {"type": 'thought', "content": c})
                        else:
                            emit('bot', {"type": 'stream', "content": c})
                    if 'tool_calls' in m: tool_calls.extend(m['tool_calls'])
                if chunk.get('done'): break
            
            if stop_event.is_set(): 
                # Partial save if stopped
                history.append({"role": "assistant", "content": full_txt + " [HALTED]"})
                save_history(sid, history)
                emit('bot', {"type": 'end'})
                break

            if tool_calls:
                history.append({"role": "assistant", "content": full_txt, "tool_calls": tool_calls})
                for t in tool_calls:
                    name, args = t['function']['name'], t['function']['arguments']
                    telemetry(f"Tool Action: {name}", "TOOL", data=args)
                    
                    res = None
                    if name == "run_shell": res = t_shell(args['cmd'])
                    elif name == "file_op": res = t_fs(args['op'], args['path'], args.get('text'))
                    elif name == "pc_control": res = t_pc(args['action'], args.get('x'), args.get('y'), args.get('text'))
                    elif name == "web_search": res = t_web(args['query'])
                    elif name == "speak": res = t_speak(args['text'])
                    
                    history.append({"role": "tool", "content": json.dumps(res)})
                continue
            else:
                history.append({"role": "assistant", "content": full_txt})
                save_history(sid, history)
                emit('bot', {"type": 'end'})
                break

    except Exception as e:
        telemetry(str(e), "ERROR")
        emit('bot', {"type": 'error', "content": str(e)})

@socketio.on('clear')
def handle_clear(sid):
    save_history(sid, [])
    emit('history', [])

if __name__ == "__main__":
    socketio.run(app, port=8080, debug=True)
