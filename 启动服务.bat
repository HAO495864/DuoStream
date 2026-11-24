@echo off
title Douyin Downloader Web
setlocal
set "PY=D:\Python\python.exe"
set "PLAYWRIGHT_BROWSERS_PATH=D:\playwright-browsers"
set "PATH=D:\ffmpeg\bin;%PATH%"
cd /d "%~dp0"
if not exist "downloads" mkdir "downloads"
"%PY%" "webapp\web_server.py"
echo.
echo Service stopped. Press any key to close.
pause >nul
endlocal
