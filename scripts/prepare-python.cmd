@echo off
setlocal EnableExtensions EnableDelayedExpansion

set "PYTHON_EXE="

rem 1) Standard Windows Python Launcher (works even when python.exe is not in PATH)
where py.exe >nul 2>nul
if not errorlevel 1 (
  for /f "usebackq delims=" %%P in (`py -3 -c "import sys; print(sys.executable)" 2^>nul`) do (
    if not defined PYTHON_EXE set "PYTHON_EXE=%%P"
  )
)

rem 2) python.exe or python3.exe already available in PATH
if not defined PYTHON_EXE (
  for %%N in (python.exe python3.exe) do (
    if not defined PYTHON_EXE (
      for /f "delims=" %%P in ('where %%N 2^>nul') do (
        if not defined PYTHON_EXE set "PYTHON_EXE=%%P"
      )
    )
  )
)

rem 3) Common per-user installation folders
if not defined PYTHON_EXE (
  for /d %%D in ("%LocalAppData%\Programs\Python\Python3*") do (
    if exist "%%~fD\python.exe" if not defined PYTHON_EXE set "PYTHON_EXE=%%~fD\python.exe"
  )
)

rem 4) Common system installation folders. Reading them does not require admin rights.
if not defined PYTHON_EXE (
  for /d %%D in ("%ProgramFiles%\Python3*") do (
    if exist "%%~fD\python.exe" if not defined PYTHON_EXE set "PYTHON_EXE=%%~fD\python.exe"
  )
)
if not defined PYTHON_EXE (
  for /d %%D in ("%ProgramFiles%\Python\Python3*") do (
    if exist "%%~fD\python.exe" if not defined PYTHON_EXE set "PYTHON_EXE=%%~fD\python.exe"
  )
)
if not defined PYTHON_EXE (
  for /d %%D in ("C:\Python3*") do (
    if exist "%%~fD\python.exe" if not defined PYTHON_EXE set "PYTHON_EXE=%%~fD\python.exe"
  )
)

if not defined PYTHON_EXE (
  echo ERROR: Python was not found in PATH or standard installation folders.
  echo Ask IT only for the exact path to python.exe; administrator rights are not required.
  exit /b 1
)

echo Found Python: !PYTHON_EXE!
"!PYTHON_EXE!" -c "import sys; print(sys.version); raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
if errorlevel 1 (
  echo ERROR: Python 3.11 or newer is required.
  exit /b 1
)

rem Create an isolated environment inside the checked-out repository.
rem This avoids PATH changes and does not install packages system-wide.
if exist ".venv\Scripts\python.exe" rmdir /s /q ".venv"
"!PYTHON_EXE!" -m venv .venv
if errorlevel 1 (
  echo ERROR: Could not create the local Python environment .venv.
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
