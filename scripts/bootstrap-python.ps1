param(
    [string]$PathFile = ""
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$RequiredMajor = 3
$RequiredMinor = 11
$InstallVersion = "3.12.10"
$InstallerUrl = "https://www.python.org/ftp/python/$InstallVersion/python-$InstallVersion-amd64.exe"
$InstallerSha256 = "67B5635E80EA51072B87941312D00EC8927C4DB9BA18938F7AD2D27B328B95FB"

function Test-PythonExecutable {
    param([string]$Exe)
    if ([string]::IsNullOrWhiteSpace($Exe) -or -not (Test-Path -LiteralPath $Exe -PathType Leaf)) {
        return $false
    }
    try {
        & $Exe -c "import sys; raise SystemExit(0 if sys.version_info >= ($RequiredMajor, $RequiredMinor) else 1)" 2>$null
        return ($LASTEXITCODE -eq 0)
    }
    catch {
        return $false
    }
}

function Publish-Python {
    param([string]$Exe)
    $Exe = (Resolve-Path -LiteralPath $Exe).Path
    $Home = Split-Path -Parent $Exe

    Write-Host "Using Python: $Exe"
    & $Exe --version
    if ($LASTEXITCODE -ne 0) {
        throw "Python executable exists but could not be started: $Exe"
    }

    if ($PathFile) {
        Set-Content -LiteralPath $PathFile -Value $Exe -Encoding ASCII
    }

    if ($env:GITHUB_ENV) {
        "PYTHON_EXE=$Exe" | Out-File -FilePath $env:GITHUB_ENV -Encoding utf8 -Append
        "PYTHON_HOME=$Home" | Out-File -FilePath $env:GITHUB_ENV -Encoding utf8 -Append
    }
    if ($env:GITHUB_PATH) {
        $Home | Out-File -FilePath $env:GITHUB_PATH -Encoding utf8 -Append
        (Join-Path $Home "Scripts") | Out-File -FilePath $env:GITHUB_PATH -Encoding utf8 -Append
    }

    return $Exe
}

# 1. Explicit interpreter from the workflow/manual environment.
if (Test-PythonExecutable $env:PYTHON_EXE) {
    Publish-Python $env:PYTHON_EXE | Out-Null
    exit 0
}

# 2. Python already available on the runner.
$pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
if ($pythonCommand -and (Test-PythonExecutable $pythonCommand.Source)) {
    Publish-Python $pythonCommand.Source | Out-Null
    exit 0
}

# 3. Reuse the job-local installation if it already exists.
$BaseDir = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } elseif ($env:TEMP) { $env:TEMP } else { $PSScriptRoot }
$InstallDir = Join-Path $BaseDir "b24-python-$InstallVersion-x64"
$LocalPython = Join-Path $InstallDir "python.exe"
if (Test-PythonExecutable $LocalPython) {
    Publish-Python $LocalPython | Out-Null
    exit 0
}

# 4. Install Python automatically for this GitHub Actions runner.
# No administrator rights and no machine-wide PATH changes are required.
Write-Host "Python $RequiredMajor.$RequiredMinor+ is not available. Installing Python $InstallVersion for this job..."
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
$Installer = Join-Path $BaseDir "python-$InstallVersion-amd64.exe"

try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $InstallerUrl -OutFile $Installer -UseBasicParsing
}
catch {
    throw "Could not download the official Python installer from python.org. URL: $InstallerUrl. $($_.Exception.Message)"
}

$ActualSha256 = (Get-FileHash -LiteralPath $Installer -Algorithm SHA256).Hash.ToUpperInvariant()
if ($ActualSha256 -ne $InstallerSha256) {
    Remove-Item -LiteralPath $Installer -Force -ErrorAction SilentlyContinue
    throw "Downloaded Python installer SHA-256 mismatch. Expected $InstallerSha256, got $ActualSha256."
}

$Signature = Get-AuthenticodeSignature -FilePath $Installer
Write-Host "Python installer Authenticode status: $($Signature.Status)"

$Arguments = @(
    "/quiet",
    "InstallAllUsers=0",
    "TargetDir=`"$InstallDir`"",
    "PrependPath=0",
    "AppendPath=0",
    "Include_launcher=0",
    "Include_test=0",
    "Include_doc=0",
    "Include_tcltk=0",
    "Include_pip=1",
    "Include_tools=1",
    "Shortcuts=0",
    "AssociateFiles=0"
)

$Process = Start-Process -FilePath $Installer -ArgumentList $Arguments -Wait -PassThru
if ($Process.ExitCode -notin @(0, 3010)) {
    throw "Python installer failed with exit code $($Process.ExitCode)."
}

Remove-Item -LiteralPath $Installer -Force -ErrorAction SilentlyContinue

if (-not (Test-PythonExecutable $LocalPython)) {
    throw ("Python installation finished, but python.exe was not found or is older than {0}.{1}: {2}" -f $RequiredMajor, $RequiredMinor, $LocalPython)
}

Publish-Python $LocalPython | Out-Null
exit 0
