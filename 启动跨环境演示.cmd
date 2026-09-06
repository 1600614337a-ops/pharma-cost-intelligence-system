@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
where docker >nul 2>&1
if errorlevel 1 (
  echo 启动失败：未找到 Docker。请先安装并启动 Docker Desktop。
  pause
  exit /b 1
)
docker info >nul 2>&1
if errorlevel 1 (
  echo 启动失败：Docker 服务尚未运行，请启动 Docker Desktop 后重试。
  pause
  exit /b 1
)
docker compose -p pharma-cost-intelligence up --build -d --wait --wait-timeout 180
if errorlevel 1 (
  echo 启动失败，请确认已安装并启动 Docker Desktop，并查看上方错误信息。
  pause
  exit /b 1
)
echo 系统已启动：http://127.0.0.1:8080
start "" "http://127.0.0.1:8080"
exit /b 0
