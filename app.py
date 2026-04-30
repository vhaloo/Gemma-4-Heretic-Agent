import os
import json
import requests
import subprocess
import base64
import logging
import psutil
import time
import pyautogui
from PIL import ImageGrab
from io import BytesIO
from flask import Flask, render_template, request, jsonify, Response
from flask_socketio import SocketIO, emit
from duckduckgo_search import DDGS

# Logging setup
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

app = Flask(__name__, template_folder='public', static_folder='public')
socketio = SocketIO(app, cors_allowed_origins="*", max_decode_packets=20)

OLLAMA_URL = "http://localhost:11434"
MODEL_NAME = "gemma-4-26B-A4B-it-heretic:latest"
WORKSPACE = "C:\\Users\\Vhaloo\\Desktop\\Gemma_Web_CLI"
HISTORY_FILE = os.path.join(WORKSPACE, "chat_history.json")
AUDIO_OUTPUT = "C:\\Users\\Vhaloo\\Desktop\\Gemini_Audio"
os.makedirs(AUDIO_OUTPUT, exist_ok=True)

# --- Tool Implementations ---

def tool_web_search(query):
    logging.info(f"Web Search: {query}")
    try:
        results = []
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=5):
                results.append(r)
        return results
    except Exception as e:
        return {"error": str(e)}

def tool_get_system_stats():
    return {
        "cpu_usage": psutil.cpu_percent(interval=1),
        "memory": psutil.virtual_memory()._asdict(),
        "disk": psutil.disk_usage('/')._asdict(),
        "processes": len(psutil.pids())
    }

def tool_speak(text, lang="en"):
    logging.info(f"Speaking: {text[:50]}...")
    # SAPI Fallback via PowerShell
    ps_cmd = f"Add-Type -AssemblyName System.Speech; $speak = New-Object System.Speech.Synthesis.SpeechSynthesizer; $speak.Speak('{text.replace("'", "''")}')"
    try:
        subprocess.Popen(["powershell", "-Command", ps_cmd])
        return "Speech initiated."
    except Exception as e:
        return {"error": str(e)}

def get_screenshot():
    try:
        screenshot = ImageGrab.grab()
        buffered = BytesIO()
        screenshot.save(buffered, format="PNG")
        return base64.b64encode(buffered.getvalue()).decode()
    except Exception as e:
        return f"Screenshot error: {str(e)}"

def tool_execute_shell(command):
    try:
        result = subprocess.run(["powershell", "-Command", command], capture_output=True, text=True, timeout=60)
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
        elif op == "delete":
            os.remove(path); return {"status": "deleted"}
    except Exception as e: return {"error": str(e)}

def tool_computer_control(action, x=None, y=None, text=None):
    try:
        if action == "click": pyautogui.click(x, y); return "Clicked at {}, {}".format(x, y)
        if action == "type": pyautogui.write(text); return "Typed: {}".format(text)
        if action == "move": pyautogui.moveTo(x, y); return "Moved to {}, {}".format(x, y)
        if action == "screenshot": return {"screenshot": get_screenshot()}
        if action == "coords": return {"x": pyautogui.position().x, "y": pyautogui.position().y}
        if action == "hotkey": pyautogui.hotkey(*text.split('+')); return f"Pressed hotkey {text}"
    except Exception as e: return {"error": str(e)}

TOOLS = [
    {"type": "function", "function": {"name": "execute_shell", "description": "Run powershell command.", "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}},
    {"type": "function", "function": {"name": "file_system", "description": "Read, write, list or delete files.", "parameters": {"type": "object", "properties": {"op": {"type": "string", "enum": ["read", "write", "list", "delete"]}, "path": {"type": "string"}, "content": {"type": "string"}}, "required": ["op", "path"]}}},
    {"type": "function", "function": {"name": "computer_control", "description": "Mouse/Keyboard/Vision.", "parameters": {"type": "object", "properties": {"action": {"type": "string", "enum": ["click", "type", "move", "screenshot", "coords", "hotkey"]}, "x": {"type": "number"}, "y": {"type": "number"}, "text": {"type": "string"}}, "required": ["action"]}}},
    {"type": "function", "function": {"name": "web_search", "description": "Search the internet for real-time info.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "get_system_stats", "description": "Get CPU, RAM and disk usage stats.", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "speak", "description": "Convert text to speech.", "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}}
]

# History Management
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
        data = resp.json()
        models = [m['name'] for m in data.get('models', [])]
        return jsonify({
            "ollama": True,
            "model": MODEL_NAME in models,
            "models": models,
            "stats": tool_get_system_stats()
        })
    except: return jsonify({"ollama": False, "model": False})

@socketio.on('user_message')
def handle_message(payload):
    global chat_history
    user_text = payload.get('message', '')
    images = payload.get('images', [])
    
    msg = {"role": "user", "content": user_text}
    if images: msg["images"] = images
    
    chat_history.append(msg)
    
    try:
        for step in range(10): # Chain limit
            emit('bot_response', {"type": 'status', "content": f"Step {step+1}: Processing..."})
            
            ollama_payload = {
                "model": MODEL_NAME,
                "messages": chat_history,
                "tools": TOOLS,
                "stream": True,
                "options": {"num_ctx": 32768, "temperature": 0.7}
            }
            
            resp = requests.post(f"{OLLAMA_URL}/api/chat", json=ollama_payload, stream=True)
            
            full_content = ""
            tool_calls = []
            
            for line in resp.iter_lines():
                if not line: continue
                chunk = json.loads(line)
                
                if 'message' in chunk:
                    msg_chunk = chunk['message']
                    
                    if 'content' in msg_chunk and msg_chunk['content']:
                        content = msg_chunk['content']
                        full_content += content
                        emit('bot_response', {"type": 'stream', "content": content})
                    
                    if 'tool_calls' in msg_chunk:
                        tool_calls.extend(msg_chunk['tool_calls'])
                
                if chunk.get('done'): break
            
            if tool_calls:
                # Add the assistant's message with tool calls to history
                assistant_msg = {"role": "assistant", "content": full_content, "tool_calls": tool_calls}
                chat_history.append(assistant_msg)
                
                for tool in tool_calls:
                    fn_name = tool['function']['name']
                    fn_args = tool['function']['arguments']
                    emit('bot_response', {"type": 'status', "content": f"Running {fn_name}..."})
                    
                    res = None
                    if fn_name == "execute_shell": res = tool_execute_shell(fn_args['command'])
                    elif fn_name == "file_system": res = tool_file_op(fn_args['op'], fn_args['path'], fn_args.get('content'))
                    elif fn_name == "computer_control": res = tool_computer_control(fn_args['action'], fn_args.get('x'), fn_args.get('y'), fn_args.get('text'))
                    elif fn_name == "web_search": res = tool_web_search(fn_args['query'])
                    elif fn_name == "get_system_stats": res = tool_get_system_stats()
                    elif fn_name == "speak": res = tool_speak(fn_args['text'])
                    
                    chat_history.append({"role": "tool", "content": json.dumps(res)})
                
                emit('bot_response', {"type": 'stream_end', "content": ""})
                continue # Loop back for next response based on tool results
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
    emit('bot_response', {"type": 'status', "content": "Memory wiped."})

if __name__ == "__main__":
    socketio.run(app, port=8080, debug=True)
