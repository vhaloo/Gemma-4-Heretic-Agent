import os
import json
import requests
import subprocess
import base64
import logging
import pyautogui
from PIL import ImageGrab
from io import BytesIO
from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit

# Logging setup
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

app = Flask(__name__, template_folder='public', static_folder='public')
socketio = SocketIO(app, cors_allowed_origins="*", max_decode_packets=10)

OLLAMA_URL = "http://localhost:11434"
MODEL_NAME = "gemma-4-26B-A4B-it-heretic:latest"
WORKSPACE = "C:\\Users\\Vhaloo\\Desktop\\Gemma_Web_CLI"
HISTORY_FILE = os.path.join(WORKSPACE, "chat_history.json")

# Tools Configuration
def get_screenshot():
    try:
        screenshot = ImageGrab.grab()
        buffered = BytesIO()
        screenshot.save(buffered, format="PNG")
        return base64.b64encode(buffered.getvalue()).decode()
    except Exception as e:
        return f"Screenshot error: {str(e)}"

def tool_execute_shell(command):
    logging.info(f"Executing shell: {command}")
    try:
        result = subprocess.run(["powershell", "-Command", command], capture_output=True, text=True, timeout=60)
        return {"stdout": result.stdout, "stderr": result.stderr, "exit_code": result.returncode}
    except Exception as e:
        return {"error": str(e)}

def tool_file_op(op, path, content=None):
    logging.info(f"File op: {op} on {path}")
    try:
        if op == "read":
            with open(path, 'r', encoding='utf-8') as f: return {"content": f.read()}
        elif op == "write":
            with open(path, 'w', encoding='utf-8') as f: f.write(content); return {"status": "success"}
        elif op == "list":
            return {"files": os.listdir(path)}
        elif op == "delete":
            os.remove(path); return {"status": "deleted"}
    except Exception as e:
        return {"error": str(e)}

def tool_computer_control(action, x=None, y=None, text=None):
    logging.info(f"Computer control: {action}")
    try:
        if action == "click": pyautogui.click(x, y); return "Clicked at {}, {}".format(x, y)
        if action == "type": pyautogui.write(text); return "Typed: {}".format(text)
        if action == "move": pyautogui.moveTo(x, y); return "Moved to {}, {}".format(x, y)
        if action == "screenshot": return {"screenshot": get_screenshot()}
        if action == "coords": return {"x": pyautogui.position().x, "y": pyautogui.position().y}
        if action == "hotkey": pyautogui.hotkey(*text.split('+')); return f"Pressed hotkey {text}"
    except Exception as e:
        return {"error": str(e)}

TOOLS = [
    {"type": "function", "function": {"name": "execute_shell", "description": "Run powershell command.", "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}},
    {"type": "function", "function": {"name": "file_system", "description": "Read, write, list or delete files.", "parameters": {"type": "object", "properties": {"op": {"type": "string", "enum": ["read", "write", "list", "delete"]}, "path": {"type": "string"}, "content": {"type": "string"}}, "required": ["op", "path"]}}},
    {"type": "function", "function": {"name": "computer_control", "description": "Control mouse (click, move, coords), keyboard (type, hotkey), or vision (screenshot).", "parameters": {"type": "object", "properties": {"action": {"type": "string", "enum": ["click", "type", "move", "screenshot", "coords", "hotkey"]}, "x": {"type": "number"}, "y": {"type": "number"}, "text": {"type": "string"}}, "required": ["action"]}}}
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
        models = [m['name'] for m in resp.json().get('models', [])]
        return jsonify({"ollama": True, "model": MODEL_NAME in models, "models": models})
    except: return jsonify({"ollama": False, "model": False, "models": []})

@app.route('/api/pull', methods=['POST'])
def pull_model():
    model = request.json.get('model')
    try:
        def generate():
            resp = requests.post(f"{OLLAMA_URL}/api/pull", json={"name": model}, stream=True)
            for line in resp.iter_lines():
                if line: yield line + b"\n"
        return app.response_class(generate(), mimetype='application/json')
    except Exception as e: return jsonify({"error": str(e)}), 500

@socketio.on('user_message')
def handle_message(payload):
    global chat_history
    user_text = payload.get('message', '')
    images = payload.get('images', [])
    
    msg = {"role": "user", "content": user_text}
    if images: msg["images"] = images
    
    chat_history.append(msg)
    emit('bot_response', {"type": 'status', "content": "Thinking..."})
    
    try:
        for _ in range(7): # Deep tool chaining
            response = requests.post(f"{OLLAMA_URL}/v1/chat/completions", json={"model": MODEL_NAME, "messages": chat_history, "tools": TOOLS, "stream": False})
            message = response.json()['choices'][0]['message']
            
            if message.get('content') and "planner" in message['content']:
                emit('bot_response', {"type": 'thought', "content": message['content']})

            if 'tool_calls' in message and message['tool_calls']:
                chat_history.append(message)
                for tool in message['tool_calls']:
                    fn_name = tool['function']['name']
                    fn_args = json.loads(tool['function']['arguments'])
                    emit('bot_response', {"type": 'status', "content": f"Invoking {fn_name}..."})
                    
                    res = None
                    if fn_name == "execute_shell": res = tool_execute_shell(fn_args['command'])
                    elif fn_name == "file_system": res = tool_file_op(fn_args['op'], fn_args['path'], fn_args.get('content'))
                    elif fn_name == "computer_control": res = tool_computer_control(fn_args['action'], fn_args.get('x'), fn_args.get('y'), fn_args.get('text'))
                    
                    chat_history.append({"role": "tool", "tool_call_id": tool['id'], "name": fn_name, "content": json.dumps(res)})
                continue
            else:
                txt = message.get('content', '')
                chat_history.append({"role": "assistant", "content": txt})
                with open(HISTORY_FILE, 'w') as f: json.dump(chat_history, f)
                emit('bot_response', {"type": 'text', "content": txt})
                break
    except Exception as e: emit('bot_response', {"type": 'error', "content": str(e)})

if __name__ == "__main__":
    socketio.run(app, port=8080, debug=True)
