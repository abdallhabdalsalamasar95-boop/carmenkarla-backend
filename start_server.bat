@echo off
setlocal
cd /d "%~dp0"

echo [CarmenKarla] Starting local Python server...

if not exist ".env" (
  echo [CarmenKarla] .env not found, creating from .env.example
  copy ".env.example" ".env" >nul
)

set "PYEXE=C:\Program Files\Python314\python.exe"
if exist "%PYEXE%" goto run

where py >nul 2>nul
if %errorlevel%==0 (
  py -3 "server.py"
  goto :eof
)

where python >nul 2>nul
if %errorlevel%==0 (
  python "server.py"
  goto :eof
)

echo [ERROR] Python not found. Please install Python 3.
exit /b 1

:run
"%PYEXE%" "server.py"
