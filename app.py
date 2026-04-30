import os
import json
import requests
import subprocess
import base64
import time
import pyautogui
from PIL import ImageGrab
from io import BytesIO
from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit
from werkzeug.utils import secure_filename

app = Flask(__name__, template_folder='public', static_folder='public')
socketio = SocketIO(app, cors_allowed_origins="*", max_decode_packets=5)

OLLAMA_URL = "http://localhost:11434"
MODEL_NAME = "gemma-4-26B-A4B-it-heretic:latest"
WORKSPACE = "C:\\Users\\Vhaloo\\Desktop\\Gemma_Web_CLI"
HISTORY_FILE = os.path.join(WORKSPACE, "chat_history.json")
UPLOAD_FOLDER = os.path.join(WORKSPACE, "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Persistent History
def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, 'r') as f:
                return json.load(f)
        except:
            return []
    return []

def save_history(history):
    with open(HISTORY_FILE, 'w') as f:
        json.dump(history, f, indent=2)

chat_history = load_history()

# Tools Configuration
def get_screenshot():
    screenshot = ImageGrab.grab()
    buffered = BytesIO()
    screenshot.save(buffered, format="PNG")
    img_str = base64.b64encode(buffered.getvalue()).decode()
    return img_str

def tool_execute_shell(command):
    try:
        result = subprocess.run(["powershell", "-Command", command], capture_output=True, text=True, timeout=30)
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
        if action == "click": pyautogui.click(x, y); return "Clicked at {}, {}".format(x, y)
        if action == "type": pyautogui.write(text); return "Typed: {}".format(text)
        if action == "move": pyautogui.moveTo(x, y); return "Moved to {}, {}".format(x, y)
        if action == "screenshot": return {"screenshot": get_screenshot()}
        if action == "coords": return {"x": pyautogui.position().x, "y": pyautogui.position().y}
    except Exception as e: return {"error": str(e)}

TOOLS = [
    {"type": "function", "function": {"name": "execute_shell", "description": "Run a powershell command.", "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}},
    {"type": "function", "function": {"name": "file_system", "description": "Read, write, or list files.", "parameters": {"type": "object", "properties": {"op": {"type": "string", "enum": ["read", "write", "list"]}, "path": {"type": "string"}, "content": {"type": "string"}}, "required": ["op", "path"]}}},
    {"type": "function", "function": {"name": "computer_control", "description": "Control mouse, keyboard, or take screenshots.", "parameters": {"type": "object", "properties": {"action": {"type": "string", "enum": ["click", "type", "move", "screenshot", "coords"]}, "x": {"type": "number"}, "y": {"type": "number"}, "text": {"type": "string"}}, "required": ["action"]}}}
]

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/status')
def status():
    try:
        resp = requests.get(f"{OLLAMA_URL}/api/tags", timeout=2)
        models = [m['name'] for m in resp.json().get('models', [])]
        return jsonify({"ollama": True, "model": MODEL_NAME in models})
    except: return jsonify({"ollama": False, "model": False})

@socketio.on('user_message')
def handle_message(payload):
    global chat_history
    user_text = payload.get('message', '')
    images = payload.get('images', []) # Base64 images
    
    current_msg = {"role": "user", "content": user_text}
    if images: current_msg["images"] = images
    
    chat_history.append(current_msg)
    emit('bot_response', {"type": 'status', "content": "Analyzing request..."})
    
    try:
        for _ in range(5): # Allow up to 5 tool chain steps
            ollama_payload = {"model": MODEL_NAME, "messages": chat_history, "tools": TOOLS, "stream": False}
            response = requests.post(f"{OLLAMA_URL}/v1/chat/completions", json=ollama_payload)
            res_json = response.json()
            message = res_json['choices'][0]['message']
            
            # Extract reasoning/thought if it exists in the message content
            if message.get('content') and "planner" in message['content']:
                emit('bot_response', {"type": 'thought', "content": message['content'].split("(End of thought process)")[0]})

            if 'tool_calls' in message and message['tool_calls']:
                chat_history.append(message)
                for tool in message['tool_calls']:
                    fn_name = tool['function']['name']
                    fn_args = json.loads(tool['function']['arguments'])
                    emit('bot_response', {"type": 'status', "content": f"Running {fn_name}..."})
                    
                    result = None
                    if fn_name == "execute_shell": result = tool_execute_shell(fn_args['command'])
                    elif fn_name == "file_system": result = tool_file_op(fn_args['op'], fn_args['path'], fn_args.get('content'))
                    elif fn_name == "computer_control": result = tool_computer_control(fn_args['action'], fn_args.get('x'), fn_args.get('y'), fn_args.get('text'))
                    
                    chat_history.append({"role": "tool", "tool_call_id": tool['id'], "name": fn_name, "content": json.dumps(result)})
                continue # Loop back to get final response
            else:
                bot_text = message.get('content', '')
                chat_history.append({"role": "assistant", "content": bot_text})
                save_history(chat_history)
                emit('bot_response', {"type": 'text', "content": bot_text})
                break
    except Exception as e:
        emit('bot_response', {"type": 'error', "content": str(e)})

@socketio.on('clear_history')
def clear_history():
    global chat_history
    chat_history = []
    if os.path.exists(HISTORY_FILE): os.remove(HISTORY_FILE)
    emit('bot_response', {"type": 'status', "content": "History cleared."})

if __name__ == "__main__":
    socketio.run(app, port=8080, debug=True)
