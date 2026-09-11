@echo off
setlocal EnableExtensions

rem Exact Python installation confirmed on this workstation.
set "PYTHON_EXE=C:\Users\Alyona.Sachyova\AppData\Local\Programs\Python\Python312\python.exe"

echo Using Python: %PYTHON_EXE%

if not exist "%PYTHON_EXE%" (
  echo ERROR: Python was not found at the configured path:
  echo %PYTHON_EXE%
  exit /b 1
)

"%PYTHON_EXE%" -c "import sys; print(sys.version); raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
if errorlevel 1 (
  echo ERROR: Python 3.11 or newer is required.
  exit /b 1
)

rem Create an isolated environment inside the checked-out repository.
rem No administrator rights or system PATH changes are required.
if exist ".venv\Scripts\python.exe" rmdir /s /q ".venv"
"%PYTHON_EXE%" -m venv .venv
if errorlevel 1 (
  echo ERROR: Python was found, but it could not create the local .venv environment.
  exit /b 1
)

".venv\Scripts\python.exe" --version
".venv\Scripts\python.exe" -m pip --version
if errorlevel 1 (
  echo ERROR: pip is unavailable in the local Python environment.
  exit /b 1
)

endlocal
exit /b 0
