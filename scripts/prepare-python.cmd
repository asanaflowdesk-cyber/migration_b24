@echo off
setlocal EnableExtensions

rem Optional override for self-hosted runners.
if defined PYTHON_EXE (
  if exist "%PYTHON_EXE%" (
    set "PYTHON_CMD=\"%PYTHON_EXE%\""
    goto :validate
  )
  echo ERROR: PYTHON_EXE is set but the file does not exist: %PYTHON_EXE%
  exit /b 1
)

rem Prefer a normal Python installation and avoid relying on one user profile.
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
  set "PYTHON_CMD=\"%LOCALAPPDATA%\Programs\Python\Python312\python.exe\""
  goto :validate
)
if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" (
  set "PYTHON_CMD=\"%LOCALAPPDATA%\Programs\Python\Python313\python.exe\""
  goto :validate
)

where py >nul 2>nul
if not errorlevel 1 (
  py -3.12 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
  if not errorlevel 1 (
    set "PYTHON_CMD=py -3.12"
    goto :validate
  )
  py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
  if not errorlevel 1 (
    set "PYTHON_CMD=py -3"
    goto :validate
  )
)

where python >nul 2>nul
if not errorlevel 1 (
  set "PYTHON_CMD=python"
  goto :validate
)

echo ERROR: Python 3.11 or newer was not found.
echo Set PYTHON_EXE to the full path of python.exe or install Python 3.11+.
exit /b 1

:validate
echo Using Python command: %PYTHON_CMD%
%PYTHON_CMD% -c "import sys; print(sys.version); raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
if errorlevel 1 (
  echo ERROR: Python 3.11 or newer is required.
  exit /b 1
)

rem Create an isolated environment in the current process directory.
if exist ".venv\Scripts\python.exe" rmdir /s /q ".venv"
%PYTHON_CMD% -m venv .venv
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
