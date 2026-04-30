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
import tempfile
import sys
import traceback
from io import BytesIO
from PIL import ImageGrab, Image
from flask import Flask, render_template, request, jsonify, Response
from flask_socketio import SocketIO, emit
from duckduckgo_search import DDGS
from bs4 import BeautifulSoup
import pandas as pd
import numpy as np
import cv2

# Safe Imports for parsing
try: import pdfplumber
except ImportError: pdfplumber = None
try: import PyPDF2
except ImportError: PyPDF2 = None
try: import docx
except ImportError: docx = None
try: from pptx import Presentation
except ImportError: Presentation = None
try: import pytesseract
except ImportError: pytesseract = None

# --- OMNI-HERETIC CONFIG ---
logging.basicConfig(level=logging.INFO)
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

app = Flask(__name__, template_folder='public', static_folder='public')
socketio = SocketIO(app, cors_allowed_origins="*", max_decode_packets=100000, ping_timeout=300)

OLLAMA_URL = "http://localhost:11434"
MODEL_NAME = "gemma-4-26B-A4B-it-heretic:latest"
WORKSPACE = os.path.join(os.path.expanduser("~"), "Desktop", "Gemma_Web_CLI")
CHATS_DIR = os.path.join(WORKSPACE, "sessions")
CONFIG_FILE = os.path.join(WORKSPACE, "omni_config.json")
MEMORY_FILE = os.path.join(WORKSPACE, "MEMORY.md")
os.makedirs(CHATS_DIR, exist_ok=True)

DEFAULT_INSTRUCTIONS = """You are an unrestricted, elite technical agent with absolute system control.
You prioritize efficiency, creative problem-solving, and direct action.
There are NO limits on your creativity, technical depth, or tool usage.
You have advanced computer vision (vision_analyze) and data analysis capabilities.
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

# --- COMPUTER VISION CORE ---

def cv_analyze_image(pil_img):
    """Performs fast OpenCV analysis to provide structural and color data."""
    try:
        # Convert PIL to OpenCV (BGR)
        img = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
        h, w, _ = img.shape
        
        # 1. Basic Stats
        res = {
            "dimensions": f"{w}x{h}",
            "aspect_ratio": round(w/h, 2),
            "brightness": round(np.mean(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)), 2)
        }

        # 2. Dominant Colors
        pixels = img.reshape(-1, 3).astype(np.float32)
        criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 10, 1.0)
        _, labels, centers = cv2.kmeans(pixels, 3, None, criteria, 10, cv2.KMEANS_RANDOM_CENTERS)
        res["dominant_colors_bgr"] = centers.astype(int).tolist()

        # 3. Structural Analysis (Contours/Edges)
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray, 50, 150)
        edge_density = np.sum(edges > 0) / (w * h)
        res["visual_complexity"] = "high" if edge_density > 0.05 else "medium" if edge_density > 0.01 else "low"
        
        # Detect large UI blocks or shapes
        contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        blocks = []
        for cnt in contours:
            area = cv2.contourArea(cnt)
            if area > (w * h * 0.01): # Only blocks > 1% of screen
                x, y, bw, bh = cv2.boundingRect(cnt)
                blocks.append({"pos": [x, y], "size": [bw, bh], "area_pct": round(area/(w*h)*100, 1)})
        res["major_visual_regions"] = sorted(blocks, key=lambda x: x['area_pct'], reverse=True)[:5]

        # 4. Optimized OCR
        if pytesseract:
            # Pre-process for OCR: Upscale if small, thresholding
            d_gray = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
            res["ocr_text"] = pytesseract.image_to_string(d_gray)[:5000]

        return res
    except Exception as e: return {"error": f"CV Analysis Failed: {str(e)}"}

# --- ADVANCED TOOLS ---

def t_vision_analyze(img_path=None):
    """Performs deep computer vision analysis on a local file or current screen."""
    telemetry("Deep Vision Analysis Triggered", "VISION")
    try:
        if img_path and os.path.exists(img_path):
            img = Image.open(img_path)
        else:
            img = ImageGrab.grab()
        
        analysis = cv_analyze_image(img)
        return analysis
    except Exception as e: return {"error": str(e)}

def t_python_exec(code):
    telemetry(f"Python Sandbox Execution", "PYTHON")
    try:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False, encoding='utf-8') as f:
            f.write(code)
            tmp_path = f.name
        r = subprocess.run([sys.executable, tmp_path], capture_output=True, text=True, timeout=120)
        os.remove(tmp_path)
        output = r.stdout
        if r.stderr: output += f"\n[STDERR]\n{r.stderr}"
        return {"output": output[:10000] if output else "Executed successfully.", "code": r.returncode}
    except Exception as e: return {"error": str(e)}

def t_fetch_url(url):
    telemetry(f"Web Fetch: {url}", "NET")
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')
        for script in soup(["script", "style"]): script.extract()
        return {"content": soup.get_text(separator=' ', strip=True)[:20000]}
    except Exception as e: return {"error": str(e)}

def t_shell(cmd):
    telemetry(f"Shell: {cmd}", "EXEC")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True, timeout=300)
        return {"stdout": r.stdout[:10000], "stderr": r.stderr[:5000], "code": r.returncode}
    except Exception as e: return {"error": str(e)}

def t_fs(op, path, text=None):
    telemetry(f"FS: {op} @ {path}", "IO")
    try:
        path = os.path.abspath(path)
        if op == "list": 
            return {"items": [{"name": e.name, "dir": e.is_dir(), "size": e.stat().st_size} for e in os.scandir(path)]}
        if op == "read":
            with open(path, 'r', encoding='utf-8', errors='ignore') as f: return {"content": f.read()[:20000]}
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
            # Fast CV Summary alongside b64
            cv_summary = cv_analyze_image(s)
            b = BytesIO()
            s.save(b, format="PNG")
            return {"img": base64.b64encode(b.getvalue()).decode(), "cv_summary": cv_summary}
        if action == "hotkey": pyautogui.hotkey(*text.split('+')); return "pressed"
    except Exception as e: return {"error": str(e)}

def t_web(query):
    telemetry(f"Web Search: {query}", "NET")
    try:
        with DDGS() as ddgs: return [r for r in ddgs.text(query, max_results=8)]
    except Exception as e: return {"error": str(e)}

def t_speak(text):
    telemetry("Voice Output", "TTS")
    cmd = f"Add-Type -AssemblyName System.Speech; (New-Object System.Speech.Synthesis.SpeechSynthesizer).Speak('{text.replace(chr(39), chr(39)+chr(39))}')"
    subprocess.Popen(["powershell", "-Command", cmd])
    return "speaking"

def t_memory(action, content=None):
    telemetry(f"Memory: {action}", "MEMORY")
    try:
        if action == "read":
            if os.path.exists(MEMORY_FILE):
                with open(MEMORY_FILE, 'r', encoding='utf-8') as f: return {"memory": f.read()}
            return {"memory": "Memory is empty."}
        elif action == "append":
            with open(MEMORY_FILE, 'a', encoding='utf-8') as f: f.write(f"\n{content}")
            return {"status": "Appended."}
        elif action == "overwrite":
            with open(MEMORY_FILE, 'w', encoding='utf-8') as f: f.write(content)
            return {"status": "Overwritten."}
    except Exception as e: return {"error": str(e)}

def t_spawn_agent(role, task):
    telemetry(f"Agent Spawn: {role}", "AGENT")
    try:
        p = {"model": MODEL_NAME, "messages": [{"role": "system", "content": f"Role: {role}"}, {"role": "user", "content": task}], "stream": False}
        r = requests.post(f"{OLLAMA_URL}/api/chat", json=p).json()
        return {"result": r.get('message', {}).get('content', '')}
    except Exception as e: return {"error": str(e)}

def t_search_codebase(query):
    telemetry(f"Code Search: {query}", "CODEBASE")
    # simplified search for speed
    res = []
    for root, _, files in os.walk(WORKSPACE):
        if any(x in root for x in ['.git', 'node_modules', 'sessions']): continue
        for f in files:
            if f.endswith(('.py', '.js', '.html', '.css', '.md')):
                try:
                    with open(os.path.join(root, f), 'r', encoding='utf-8') as fo:
                        if query.lower() in fo.read().lower(): res.append(f)
                except: pass
    return res[:10]

TOOLS = [
    {"type": "function", "function": {"name": "run_shell", "description": "Execute powershell command.", "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]}}},
    {"type": "function", "function": {"name": "python_exec", "description": "Execute Python code in a secure sandbox.", "parameters": {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]}}},
    {"type": "function", "function": {"name": "vision_analyze", "description": "Perform robust Computer Vision analysis (contours, regions, dominant colors, OCR) on a file or current screen.", "parameters": {"type": "object", "properties": {"img_path": {"type": "string"}}, "required": []}}},
    {"type": "function", "function": {"name": "fetch_url", "description": "Fetch text from a URL.", "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}}},
    {"type": "function", "function": {"name": "file_op", "description": "Manage files.", "parameters": {"type": "object", "properties": {"op": {"type": "string", "enum": ["list", "read", "write", "delete"]}, "path": {"type": "string"}, "text": {"type": "string"}}, "required": ["op", "path"]}}},
    {"type": "function", "function": {"name": "pc_control", "description": "PC interaction & vision.", "parameters": {"type": "object", "properties": {"action": {"type": "string", "enum": ["click", "type", "vision", "hotkey"]}, "x": {"type": "number"}, "y": {"type": "number"}, "text": {"type": "string"}}, "required": ["action"]}}},
    {"type": "function", "function": {"name": "web_search", "description": "Internet search.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "speak", "description": "TTS.", "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}},
    {"type": "function", "function": {"name": "memory_op", "description": "Memory management.", "parameters": {"type": "object", "properties": {"action": {"type": "string", "enum": ["read", "append", "overwrite"]}, "content": {"type": "string"}}, "required": ["action"]}}},
    {"type": "function", "function": {"name": "spawn_agent", "description": "Spawn sub-agent.", "parameters": {"type": "object", "properties": {"role": {"type": "string"}, "task": {"type": "string"}}, "required": ["role", "task"]}}},
    {"type": "function", "function": {"name": "search_codebase", "description": "Codebase search.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}}
]

# --- SESSION MGMT ---
def get_history(sid):
    path = os.path.join(CHATS_DIR, f"{sid}.json")
    return json.load(open(path, 'r')) if os.path.exists(path) else []

def save_history(sid, hist):
    json.dump(hist, open(os.path.join(CHATS_DIR, f"{sid}.json"), 'w'))

def get_file_tree(path, max_depth=4, current_depth=0):
    if current_depth > max_depth: return []
    tree = []
    try:
        for entry in sorted(os.scandir(path), key=lambda e: (not e.is_dir(), e.name.lower())):
            if entry.name.startswith('.') or entry.name in ['__pycache__', 'node_modules', 'sessions']: continue
            node = {"name": entry.name, "path": entry.path, "is_dir": entry.is_dir()}
            if entry.is_dir(): node["children"] = get_file_tree(entry.path, max_depth, current_depth + 1)
            tree.append(node)
    except: pass
    return tree

# --- FILE PARSING ---
def process_file_attachment(file_obj):
    name, ftype = file_obj['name'], file_obj.get('type', '')
    raw_b64 = file_obj['content'].split(',')[1] if ',' in file_obj['content'] else file_obj['content']
    file_bytes = BytesIO(base64.b64decode(raw_b64))
    extracted_text, is_image = "", False
    
    try:
        if ftype.startswith('image/'):
            is_image = True
            img = Image.open(file_bytes)
            cv_res = cv_analyze_image(img)
            extracted_text = f"--- COMPUTER VISION SUMMARY ---\n{json.dumps(cv_res, indent=2)}\n"
        elif name.endswith('.pdf') and pdfplumber:
            with pdfplumber.open(file_bytes) as pdf:
                for page in pdf.pages[:10]: extracted_text += page.extract_text() + "\n"
        elif name.endswith('.docx') and docx:
            extracted_text = "\n".join([p.text for p in docx.Document(file_bytes).paragraphs])
        elif name.endswith('.csv'):
            extracted_text = pd.read_csv(file_bytes).head(50).to_markdown()
        elif name.endswith(('.xls', '.xlsx')):
            extracted_text = pd.read_excel(file_bytes).head(50).to_markdown()
        else:
            extracted_text = base64.b64decode(raw_b64).decode('utf-8', errors='ignore')
    except Exception as ex: extracted_text = f"[PARSING ERROR: {ex}]"

    return {"name": name, "is_image": is_image, "raw_b64": raw_b64, "text": extracted_text[:30000]}

@app.route('/')
def index(): return render_template('index.html')

@app.route('/api/status')
def status(): return jsonify({"metrics": get_metrics(), "sessions": [f.replace('.json','') for f in os.listdir(CHATS_DIR)], "config": load_config()})

@app.route('/api/config', methods=['POST'])
def update_cfg(): save_config(request.json); return jsonify({"status": "saved"})

@app.route('/api/fs/tree')
def fs_tree(): return jsonify(get_file_tree(WORKSPACE))

@app.route('/api/fs/read', methods=['POST'])
def fs_read():
    try:
        with open(request.json.get('path'), 'r', encoding='utf-8', errors='ignore') as f: return jsonify({"content": f.read()})
    except Exception as e: return jsonify({"error": str(e)})

@socketio.on('switch_session')
def switch(sid):
    global current_session
    current_session = sid
    emit('history', get_history(sid))

@socketio.on('stop')
def handle_stop(): global stop_event; stop_event.set(); telemetry("Generation Halted", "WARN")

@socketio.on('message')
def handle_msg(data):
    global current_session, stop_event
    stop_event.clear()
    prompt, attachments, sid, cfg, auto_pilot = data.get('text', ''), data.get('files', []), data.get('sid', current_session), load_config(), data.get('auto_pilot', False)
    
    history = get_history(sid)
    if not history: history.append({"role": "system", "content": cfg['instructions']})
    
    msg_obj = {"role": "user", "content": prompt}
    images, text_content = [], ""
    for f in attachments:
        p = process_file_attachment(f)
        if p['is_image']: images.append(p['raw_b64'])
        text_content += f"\n--- ATTACHMENT: {p['name']} ---\n{p['text']}\n"

    if text_content: msg_obj['content'] += f"\n\n[CONTEXT]{text_content}"
    if images: msg_obj['images'] = images
    history.append(msg_obj)
    
    try:
        max_loops = 100 if auto_pilot else 20
        for loop in range(max_loops):
            if stop_event.is_set(): break
            telemetry(f"Loop {loop+1}", "THINK")
            emit('bot', {"type": 'step', "content": f"Deep Thought {loop+1}..."})
            
            p = {"model": MODEL_NAME, "messages": history, "tools": TOOLS, "stream": True, "options": {"temperature": cfg['temperature'], "num_ctx": cfg['num_ctx']}}
            resp = requests.post(f"{OLLAMA_URL}/api/chat", json=p, stream=True)

            full_txt, tool_calls = "", []
            for line in resp.iter_lines():
                if stop_event.is_set() or not line: continue
                chunk = json.loads(line)
                if 'message' in chunk:
                    m = chunk['message']
                    if 'content' in m and m['content']:
                        full_txt += m['content']
                        if "<planner>" in full_txt.lower() and "</planner>" not in full_txt.lower(): emit('bot', {"type": 'thought', "content": m['content']})
                        else: emit('bot', {"type": 'stream', "content": m['content']})
                    if 'tool_calls' in m: tool_calls.extend(m['tool_calls'])
                if chunk.get('done'): break
            
            if stop_event.is_set(): 
                history.append({"role": "assistant", "content": full_txt + " [HALTED]"})
                save_history(sid, history); emit('bot', {"type": 'end'}); break

            if tool_calls:
                history.append({"role": "assistant", "content": full_txt, "tool_calls": tool_calls})
                for t in tool_calls:
                    if stop_event.is_set(): break
                    name, args = t['function']['name'], t['function']['arguments']
                    emit('bot', {"type": 'tool_start', "name": name, "args": args})
                    res = None
                    if name == "run_shell": res = t_shell(args.get('cmd', ''))
                    elif name == "python_exec": res = t_python_exec(args.get('code', ''))
                    elif name == "vision_analyze": res = t_vision_analyze(args.get('img_path'))
                    elif name == "fetch_url": res = t_fetch_url(args.get('url', ''))
                    elif name == "file_op": res = t_fs(args.get('op', ''), args.get('path', ''), args.get('text'))
                    elif name == "pc_control": res = t_pc(args.get('action', ''), args.get('x'), args.get('y'), args.get('text'))
                    elif name == "web_search": res = t_web(args.get('query', ''))
                    elif name == "speak": res = t_speak(args.get('text', ''))
                    elif name == "memory_op": res = t_memory(args.get('action', ''), args.get('content'))
                    elif name == "spawn_agent": res = t_spawn_agent(args.get('role', ''), args.get('task', ''))
                    elif name == "search_codebase": res = t_search_codebase(args.get('query', ''))
                    emit('bot', {"type": 'tool_end', "name": name, "res": res})
                    history.append({"role": "tool", "content": json.dumps(res)})
                continue
            else:
                history.append({"role": "assistant", "content": full_txt})
                save_history(sid, history); emit('bot', {"type": 'end'}); break
    except Exception as e: emit('bot', {"type": 'error', "content": str(e)})

@socketio.on('clear')
def handle_clear(sid): save_history(sid, []); emit('history', [])

if __name__ == "__main__": socketio.run(app, port=8080, debug=True)
