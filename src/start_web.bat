@echo off
chcp 65001 >nul
echo 正在清理 8765 端口上的旧 web_server 进程...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8765" ^| findstr "LISTENING"') do (
    taskkill /PID %%a /F >nul 2>&1
)
timeout /t 1 /nobreak >nul
echo 启动 web_server ...
python "%~dp0web_server.py"
pause
