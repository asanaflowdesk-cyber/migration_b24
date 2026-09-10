@echo off
setlocal EnableExtensions

rem GitHub Actions workflows install Python with actions/setup-python before
rem calling this script. PYTHON_EXE remains available for local/manual runs.
if defined PYTHON_EXE (
  if not exist "%PYTHON_EXE%" (
    echo ERROR: PYTHON_EXE is set but the file does not exist: %PYTHON_EXE%
    exit /b 1
  )
  set "PYTHON_RESOLVED=%PYTHON_EXE%"
  goto :validate
)

rem setup-python prepends its interpreter to PATH. Resolve that interpreter
rem instead of relying on a Python installation tied to the runner account.
for /f "delims=" %%P in ('where python 2^>nul') do (
  set "PYTHON_RESOLVED=%%P"
  goto :validate
)

echo ERROR: Python is not available on PATH.
echo GitHub Actions must run actions/setup-python before this script.
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
