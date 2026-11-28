@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"

rem ---- locate Python -------------------------------------------------------
where py >nul 2>nul
if not errorlevel 1 (
  set "PY=py"
) else (
  set "PY=python"
)
%PY% --version >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python 3.11+ not found. Install Python and try again.
  pause
  exit /b 1
)

if not exist "pyproject.toml" (
  echo [ERROR] start.bat must be run from the conversation_AS folder.
  pause
  exit /b 1
)

if "%~1"=="--help" (
  %PY% -m agentorchestrator ui --help
  exit /b 0
)

rem ---- web dependencies ----------------------------------------------------
%PY% -c "import fastapi, uvicorn" >nul 2>nul
if errorlevel 1 (
  echo [INFO] Installing web dependencies: fastapi, uvicorn ...
  %PY% -m pip install -e ".[web]"
  if errorlevel 1 (
    echo [ERROR] Dependency installation failed. Check your network / pip.
    pause
    exit /b 1
  )
)

rem ---- WorkBuddy warning ---------------------------------------------------
tasklist /FI "IMAGENAME eq WorkBuddy.exe" 2>nul | find /I "WorkBuddy.exe" >nul
if errorlevel 1 (
  echo [WARN] WorkBuddy.exe is not running.
  echo        The workbuddy agent will be unavailable until you start it.
)

rem ---- OpenCode password ---------------------------------------------------
if not defined OPENCODE_SERVER_PASSWORD (
  if exist "data\opencode-password.txt" (
    set /p OPENCODE_SERVER_PASSWORD=<"data\opencode-password.txt"
  ) else (
    for /f "usebackq delims=" %%i in (`powershell -NoProfile -Command "[guid]::NewGuid().ToString()"`) do set "OPENCODE_SERVER_PASSWORD=%%i"
  )
)
if not exist "data" mkdir "data"
>"data\opencode-password.txt" echo %OPENCODE_SERVER_PASSWORD%

rem ---- dedicated OpenCode server on 4097 -----------------------------------
set "OPENCODE_BASE_URL=http://127.0.0.1:4097"
powershell -NoProfile -Command "if (Get-NetTCPConnection -State Listen -LocalPort 4097 -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }" >nul 2>nul
if errorlevel 1 (
  where opencode >nul 2>nul
  if errorlevel 1 (
    echo [WARN] opencode CLI not found. UI will use %OPENCODE_BASE_URL% and may fail health checks.
  ) else (
    echo [INFO] Starting OpenCode on 127.0.0.1:4097 ...
    start "OpenCode 4097" cmd /k "set OPENCODE_SERVER_PASSWORD=%OPENCODE_SERVER_PASSWORD%&& opencode serve --hostname 127.0.0.1 --port 4097"
    powershell -NoProfile -Command "$ok=$false; for($i=0;$i -lt 60;$i++){ Start-Sleep -Milliseconds 500; if(Get-NetTCPConnection -State Listen -LocalPort 4097 -ErrorAction SilentlyContinue){$ok=$true;break} }; if($ok){exit 0}else{exit 1}" >nul 2>nul
    if errorlevel 1 echo [WARN] OpenCode did not start within 30s. Check the OpenCode window.
  )
)

rem ---- already running? ----------------------------------------------------
if "%~1"=="" (
  powershell -NoProfile -Command "if (Get-NetTCPConnection -State Listen -LocalPort 8765 -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }" >nul 2>nul
  if not errorlevel 1 (
    echo [INFO] UI is already running: http://127.0.0.1:8765/
    start "" "http://127.0.0.1:8765/"
    exit /b 0
  )
)

rem ---- start UI ------------------------------------------------------------
echo.
echo [INFO] Starting UI (default http://127.0.0.1:8765/) ...
echo        WorkBuddy desktop must be running and logged in for the workbuddy agent.
echo        Press Ctrl+C to stop the UI.
echo.
%PY% -m agentorchestrator ui %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo.
  echo [ERROR] UI exited with code %RC%.
  pause
)
exit /b %RC%
