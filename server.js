const express = require('express');
const http = require('http');
const { Server } = require('socket.io');
const os = require('os');
const pty = require('node-pty');

const app = express();
const server = http.createServer(app);
const io = new Server(server);

app.use(express.static('public'));

app.get('/api/status', async (req, res) => {
    try {
        const response = await fetch('http://localhost:11434/api/tags');
        if (!response.ok) throw new Error('Ollama not running');
        const data = await response.json();
        const hasModel = data.models.some(m => m.name === 'gemma-4-26B-A4B-it-heretic:latest');
        
        res.json({
            ollamaRunning: true,
            modelFound: hasModel,
            models: data.models.map(m => m.name)
        });
    } catch (e) {
        res.json({
            ollamaRunning: false,
            modelFound: false,
            error: e.message
        });
    }
});

io.on('connection', (socket) => {
    console.log('Client connected');
    
    // Determine shell based on OS
    const shell = os.platform() === 'win32' ? 'powershell.exe' : 'bash';
    
    // Provide a short delay and then launch gemini. Using powershell -NoExit ensures the shell stays open.
    const args = os.platform() === 'win32' ? ['-NoLogo', '-NoExit', '-Command', 'gemini --model gemma-4-26B-A4B-it-heretic:latest'] : ['-c', 'gemini --model gemma-4-26B-A4B-it-heretic:latest; exec bash'];
    
    const ptyProcess = pty.spawn(shell, args, {
        name: 'xterm-256color',
        cols: 120,
        rows: 40,
        cwd: process.env.HOME || process.env.USERPROFILE,
        env: process.env
    });

    ptyProcess.onData((data) => {
        socket.emit('output', data);
    });

    socket.on('input', (data) => {
        ptyProcess.write(data);
    });

    socket.on('resize', (size) => {
        try {
            ptyProcess.resize(size.cols, size.rows);
        } catch (e) {
            console.error('Resize error:', e);
        }
    });

    socket.on('disconnect', () => {
        console.log('Client disconnected');
        ptyProcess.kill();
    });
});

const PORT = 8080;
server.listen(PORT, () => {
    console.log(`Server running on http://localhost:${PORT}`);
});
