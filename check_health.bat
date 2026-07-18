@echo off
setlocal
cd /d "%~dp0"

set "BASE_URL=%~1"
if "%BASE_URL%"=="" set "BASE_URL=http://127.0.0.1:8080"

echo [CarmenKarla] Checking health: %BASE_URL%/health
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $r = Invoke-RestMethod -Uri '%BASE_URL%/health' -Method Get -TimeoutSec 5; $r | ConvertTo-Json -Depth 5; exit 0 } catch { Write-Host $_.Exception.Message; exit 1 }"
