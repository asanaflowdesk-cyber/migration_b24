@echo off
setlocal EnableExtensions

rem Resolve Python 3.11+. On GitHub Actions this script can bootstrap a local
rem Python automatically if the runner has no interpreter installed.
if defined PYTHON_EXE (
  if exist "%PYTHON_EXE%" (
    set "PYTHON_RESOLVED=%PYTHON_EXE%"
    goto :validate
  )
)

if defined pythonLocation (
  if exist "%pythonLocation%\python.exe" (
    set "PYTHON_RESOLVED=%pythonLocation%\python.exe"
    goto :validate
  )
)
if defined Python3_ROOT_DIR (
  if exist "%Python3_ROOT_DIR%\python.exe" (
    set "PYTHON_RESOLVED=%Python3_ROOT_DIR%\python.exe"
    goto :validate
  )
)

for /f "delims=" %%P in ('where python 2^>nul') do (
  set "PYTHON_RESOLVED=%%P"
  goto :validate
)

rem Self-healing fallback for GitHub Actions/self-hosted Windows runners.
rem bootstrap-python.ps1 downloads the signed official CPython installer and
rem installs it only into RUNNER_TEMP; no admin rights or machine PATH needed.
if /I "%GITHUB_ACTIONS%"=="true" (
  set "_PY_PATH_FILE=%TEMP%\b24-python-path-%RANDOM%-%RANDOM%.txt"
  powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0bootstrap-python.ps1" -PathFile "%_PY_PATH_FILE%"
  if errorlevel 1 (
    echo ERROR: Automatic Python bootstrap failed.
    if exist "%_PY_PATH_FILE%" del /q "%_PY_PATH_FILE%" >nul 2>&1
    exit /b 1
  )
  if exist "%_PY_PATH_FILE%" (
    set /p PYTHON_RESOLVED=<"%_PY_PATH_FILE%"
    del /q "%_PY_PATH_FILE%" >nul 2>&1
  )
  if defined PYTHON_RESOLVED goto :validate
)

echo ERROR: Python 3.11+ could not be resolved.
echo GitHub Actions should bootstrap it automatically.
echo For a manual run, set PYTHON_EXE to the full path of Python 3.11+ python.exe.
exit /b 1

:validate
echo Using Python: %PYTHON_RESOLVED%
"%PYTHON_RESOLVED%" -c "import sys; print(sys.version); raise SystemExit(0 if sys.version_info ^>= (3, 11) else 1)"
if errorlevel 1 (
  echo ERROR: Python 3.11 or newer is required.
  exit /b 1
)

rem Recreate an isolated environment for this job. Self-hosted workspaces can
rem persist between runs, so reusing a previous .venv is unsafe.
if exist ".venv\Scripts\python.exe" rmdir /s /q ".venv"
if exist ".venv" rmdir /s /q ".venv"
"%PYTHON_RESOLVED%" -m venv .venv
if errorlevel 1 (
  echo ERROR: Python is available, but creating .venv failed.
  exit /b 1
)

".venv\Scripts\python.exe" --version
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" -m pip --version
if errorlevel 1 (
  echo ERROR: pip is unavailable in the local Python environment.
  exit /b 1
)

endlocal
exit /b 0
