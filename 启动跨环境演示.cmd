@echo off
chcp 65001 >nul
cd /d "%~dp0"
docker compose up --build -d
if errorlevel 1 (
  echo 启动失败，请确认已安装并启动 Docker Desktop。
  pause
  exit /b 1
)
echo 系统正在启动。首次构建可能需要数分钟，请稍后访问 http://127.0.0.1:8080
start "" "http://127.0.0.1:8080"
