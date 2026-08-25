@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\configure_qwen.ps1" %*
set "EXIT_CODE=%ERRORLEVEL%"
echo(
if not "%EXIT_CODE%"=="0" echo Qwen configuration failed. Review the error above.
if "%EXIT_CODE%"=="0" echo Qwen configuration saved. Restart the demo services before use.
if /i "%~1"=="-ValidateOnly" exit /b %EXIT_CODE%
pause
exit /b %EXIT_CODE%
