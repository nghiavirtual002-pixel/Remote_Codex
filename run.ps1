$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

function Get-DotEnvValue([string]$key) {
    $line = Get-Content ".env" | Where-Object { $_ -match "^\s*$key\s*=" } | Select-Object -First 1
    if (-not $line) {
        return $null
    }
    $value = $line -replace "^\s*$key\s*=\s*", ""
    return $value.Trim()
}

function Test-PythonExecutable([string]$pythonPath) {
    if (-not (Test-Path $pythonPath)) {
        return $false
    }
    & $pythonPath --version *> $null
    return ($LASTEXITCODE -eq 0)
}

if (-not (Test-Path ".env")) {
    throw "Missing .env file. Create it and fill TELEGRAM_BOT_TOKEN plus REPO_PATH or REPO_PATHS first."
}

$envText = Get-Content ".env" -Raw
if ($envText -match "PUT_TOKEN_HERE" -or $envText -match "PUT_CHAT_ID_HERE" -or $envText -match "D:/path/to/your/local/git/repo") {
    throw "Please update placeholders in .env before running."
}

$pythonVersion = Get-DotEnvValue "PYTHON_VERSION"
if ([string]::IsNullOrWhiteSpace($pythonVersion)) {
    $pythonVersion = "3.11"
}

$pyLauncher = Get-Command py -ErrorAction SilentlyContinue
if (-not $pyLauncher) {
    throw "Python launcher 'py' is not available. Install Python $pythonVersion and retry."
}

$venvPython = Join-Path $root ".venv\Scripts\python.exe"
if ((Test-Path $venvPython) -and (-not (Test-PythonExecutable $venvPython))) {
    Write-Host "[setup] Existing .venv is broken. Recreating..."
    Remove-Item -Recurse -Force ".venv"
}

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "[setup] Creating virtual environment (.venv) with Python $pythonVersion..."
    & py "-$pythonVersion" -m venv .venv
    if ($LASTEXITCODE -ne 0) {
        $installed = (& py -0 2>$null | Out-String).Trim()
        throw "Could not create venv with Python $pythonVersion. Installed versions:`n$installed`nSet PYTHON_VERSION in .env (for example 3.10) or install Python $pythonVersion."
    }
    $venvPython = Join-Path $root ".venv\Scripts\python.exe"
}
if (-not (Test-PythonExecutable $venvPython)) {
    throw "Virtual environment python not found at $venvPython"
}

$depsMarker = ".venv\.deps_installed"
$requirementsPath = "ai_dev_agent\requirements.txt"
$needsInstall = $true
if ((Test-Path $depsMarker) -and (Test-Path $requirementsPath)) {
    $needsInstall = ((Get-Item $depsMarker).LastWriteTimeUtc -lt (Get-Item $requirementsPath).LastWriteTimeUtc)
}
if ($needsInstall) {
    Write-Host "[setup] Installing dependencies..."
    & $venvPython -m pip install --upgrade pip
    & $venvPython -m pip install -r $requirementsPath
    New-Item -Path $depsMarker -ItemType File -Force | Out-Null
}

Write-Host "[run] Starting Telegram bot..."
& $venvPython "ai_dev_agent\bot.py"
