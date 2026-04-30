# Setup script for Gemma 4 Heretic Agent

Write-Host "--- Checking Python ---" -ForegroundColor Cyan
if (!(Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Host "[!] Python not found! Please install Python 3.10+ from python.org" -ForegroundColor Red
    exit 1
}

Write-Host "--- Checking Ollama ---" -ForegroundColor Cyan
if (!(Get-Command ollama -ErrorAction SilentlyContinue)) {
    Write-Host "[!] Ollama not found! Please install it from ollama.com" -ForegroundColor Red
    exit 1
}

Write-Host "--- Installing Python Dependencies ---" -ForegroundColor Cyan
python -m pip install --upgrade pip
python -m pip install flask flask-socketio requests psutil pyautogui Pillow 

Write-Host "--- Checking for Gemma 4 Heretic Model ---" -ForegroundColor Cyan
$models = ollama list
if ($models -like "*gemma-4-26B-A4B-it-heretic*") {
    Write-Host "[+] Model 'gemma-4-26B-A4B-it-heretic' is already installed." -ForegroundColor Green
} else {
    Write-Host "[?] Model not found in Ollama. Looking for GGUF file..." -ForegroundColor Yellow
    $ggufPath = "C:\Users\Vhaloo\Downloads\gemma-4-26B-A4B-it-heretic.Q4_K_M.gguf"
    if (Test-Path $ggufPath) {
        Write-Host "[+] Found GGUF at $ggufPath. Registering with Ollama..." -ForegroundColor Green
        $modelfile = "FROM $ggufPath"
        $modelfile | Out-File -FilePath "$PSScriptRoot\GemmaHeretic.Modelfile" -Encoding utf8
        ollama create gemma-4-26B-A4B-it-heretic:latest -f "$PSScriptRoot\GemmaHeretic.Modelfile"
        Remove-Item "$PSScriptRoot\GemmaHeretic.Modelfile"
    } else {
        Write-Host "[!] GGUF file not found at $ggufPath." -ForegroundColor Red
        Write-Host "Please ensure your GGUF is in your Downloads folder or update the path in this script." -ForegroundColor Yellow
    }
}

Write-Host "--- Initializing Git Repository ---" -ForegroundColor Cyan
if (Test-Path "$PSScriptRoot\.git") {
    Write-Host "[+] Git repo already initialized." -ForegroundColor Green
} else {
    git init
    git config user.name "Vhaloo"
    git config user.email "vhaloo@example.com"
    "__pycache__/", "uploads/", "chat_history.json" | Out-File .gitignore
    git add .
    git commit -m "Initial automated setup - Signed by Vhaloo"
}

Write-Host "`n[DONE] Environment is ready." -ForegroundColor Green
