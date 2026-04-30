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
from PIL import ImageGrab
from io import BytesIO
from flask import Flask, render_template, request, jsonify, Response
from flask_socketio import SocketIO, emit
from duckduckgo_search import DDGS

# Logging setup
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

app = Flask(__name__, template_folder='public', static_folder='public')
socketio = SocketIO(app, cors_allowed_origins="*", max_decode_packets=50)

OLLAMA_URL = "http://localhost:11434"
MODEL_NAME = "gemma-4-26B-A4B-it-heretic:latest"
WORKSPACE = "C:\\Users\\Vhaloo\\Desktop\\Gemma_Web_CLI"
HISTORY_FILE = os.path.join(WORKSPACE, "chat_history.json")

# --- Enhanced Tool Implementations ---

def tool_get_system_stats():
    gpu_stats = []
    try:
        gpus = GPUtil.getGPUs()
        for gpu in gpus:
            gpu_stats.append({
                "id": gpu.id,
                "name": gpu.name,
                "load": gpu.load * 100,
                "memory_used": gpu.memoryUsed,
                "memory_total": gpu.memoryTotal,
                "temp": gpu.temperature
            })
    except: pass
    
    return {
        "cpu_usage": psutil.cpu_percent(interval=None),
        "memory": psutil.virtual_memory()._asdict(),
        "disk": psutil.disk_usage('/')._asdict(),
        "gpus": gpu_stats,
        "timestamp": time.time()
    }

def tool_web_search(query):
    logging.info(f"Web Search: {query}")
    try:
        results = []
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=8):
                results.append(r)
        return results
    except Exception as e: return {"error": str(e)}

def tool_speak(text):
    ps_cmd = f"Add-Type -AssemblyName System.Speech; $speak = New-Object System.Speech.Synthesis.SpeechSynthesizer; $speak.Speak('{text.replace("'", "''")}')"
    subprocess.Popen(["powershell", "-Command", ps_cmd])
    return "Speaking..."

def tool_execute_shell(command):
    logging.info(f"Shell CMD: {command}")
    try:
        result = subprocess.run(["powershell", "-Command", command], capture_output=True, text=True, timeout=120)
        return {"stdout": result.stdout, "stderr": result.stderr, "exit_code": result.returncode}
    except Exception as e: return {"error": str(e)}

def tool_file_op(op, path, content=None):
    try:
        if op == "read":
            with open(path, 'r', encoding='utf-8') as f: return {"content": f.read()}
        elif op == "write":
            with open(path, 'w', encoding='utf-8') as f: f.write(content); return {"status": "success"}
        elif op == "list":
            return {"files": os.listdir(path)}
    except Exception as e: return {"error": str(e)}

def tool_computer_control(action, x=None, y=None, text=None):
    try:
        if action == "click": pyautogui.click(x, y); return f"Clicked {x},{y}"
        if action == "type": pyautogui.write(text); return f"Typed text"
        if action == "screenshot":
            screenshot = ImageGrab.grab()
            buffered = BytesIO()
            screenshot.save(buffered, format="PNG")
            return {"screenshot": base64.b64encode(buffered.getvalue()).decode()}
        return "Action completed"
    except Exception as e: return {"error": str(e)}

TOOLS = [
    {"type": "function", "function": {"name": "execute_shell", "description": "Execute a powershell command.", "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}},
    {"type": "function", "function": {"name": "file_system", "description": "Manage local files.", "parameters": {"type": "object", "properties": {"op": {"type": "string", "enum": ["read", "write", "list"]}, "path": {"type": "string"}, "content": {"type": "string"}}, "required": ["op", "path"]}}},
    {"type": "function", "function": {"name": "computer_control", "description": "Desktop control and vision.", "parameters": {"type": "object", "properties": {"action": {"type": "string", "enum": ["click", "type", "screenshot"]}, "x": {"type": "number"}, "y": {"type": "number"}, "text": {"type": "string"}}, "required": ["action"]}}},
    {"type": "function", "function": {"name": "web_search", "description": "Search the internet.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "get_system_stats", "description": "Get detailed CPU/GPU/RAM stats.", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "speak", "description": "Convert text to speech.", "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}}
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
    try:
        resp = requests.get(f"{OLLAMA_URL}/api/tags", timeout=2)
        models = [m['name'] for m in resp.json().get('models', [])]
        return jsonify({
            "ollama": True,
            "model": MODEL_NAME in models,
            "stats": tool_get_system_stats()
        })
    except: return jsonify({"ollama": False})

@socketio.on('user_message')
def handle_message(payload):
    global chat_history
    user_text = payload.get('message', '')
    images = payload.get('images', [])
    options = payload.get('options', {"temperature": 0.7, "num_ctx": 32768})
    
    msg = {"role": "user", "content": user_text}
    if images: msg["images"] = images
    
    chat_history.append(msg)
    
    try:
        for step in range(12): # High-depth reasoning
            emit('bot_response', {"type": 'status', "content": f"Neural Layer {step+1}: Processing..."})
            
            payload = {
                "model": MODEL_NAME,
                "messages": chat_history,
                "tools": TOOLS,
                "stream": True,
                "options": options
            }
            
            resp = requests.post(f"{OLLAMA_URL}/api/chat", json=payload, stream=True)
            
            full_content = ""
            tool_calls = []
            
            for line in resp.iter_lines():
                if not line: continue
                chunk = json.loads(line)
                
                if 'message' in chunk:
                    m = chunk['message']
                    if 'content' in m and m['content']:
                        content = m['content']
                        full_content += content
                        # Detection of "thinking" vs "final response"
                        if "planner" in full_content.lower() and "(end of thought process)" not in full_content.lower():
                             emit('bot_response', {"type": 'thought_stream', "content": content})
                        else:
                             emit('bot_response', {"type": 'stream', "content": content})
                    
                    if 'tool_calls' in m:
                        tool_calls.extend(m['tool_calls'])
                
                if chunk.get('done'): break
            
            if tool_calls:
                chat_history.append({"role": "assistant", "content": full_content, "tool_calls": tool_calls})
                for tool in tool_calls:
                    fn = tool['function']['name']
                    args = tool['function']['arguments']
                    emit('bot_response', {"type": 'status', "content": f"Executing {fn}..."})
                    
                    res = None
                    if fn == "execute_shell": res = tool_execute_shell(args['command'])
                    elif fn == "file_system": res = tool_file_op(args['op'], args['path'], args.get('content'))
                    elif fn == "computer_control": res = tool_computer_control(args['action'], args.get('x'), args.get('y'), args.get('text'))
                    elif fn == "web_search": res = tool_web_search(args['query'])
                    elif fn == "get_system_stats": res = tool_get_system_stats()
                    elif fn_name == "speak": res = tool_speak(args['text'])
                    
                    chat_history.append({"role": "tool", "content": json.dumps(res)})
                continue
            else:
                chat_history.append({"role": "assistant", "content": full_content})
                with open(HISTORY_FILE, 'w') as f: json.dump(chat_history, f)
                emit('bot_response', {"type": 'stream_end', "content": ""})
                break

    except Exception as e:
        emit('bot_response', {"type": 'error', "content": str(e)})

@socketio.on('clear_history')
def clear_history():
    global chat_history
    chat_history = []
    if os.path.exists(HISTORY_FILE): os.remove(HISTORY_FILE)
    emit('bot_response', {"type": 'status', "content": "System Purged."})

if __name__ == "__main__":
    socketio.run(app, port=8080, debug=True)
