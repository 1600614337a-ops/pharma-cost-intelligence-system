@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
docker compose -p pharma-cost-intelligence down
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" pause
exit /b %EXIT_CODE%
