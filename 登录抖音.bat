@echo off
title Douyin Login
setlocal
set "PY=D:\Python\python.exe"
set "PLAYWRIGHT_BROWSERS_PATH=D:\playwright-browsers"
set "PATH=D:\ffmpeg\bin;%PATH%"
cd /d "%~dp0engine"
echo A browser window will open. Scan the QR code to log in, then press Enter here.
"%PY%" -m tools.cookie_fetcher --config config.yml
echo.
pause >nul
endlocal
