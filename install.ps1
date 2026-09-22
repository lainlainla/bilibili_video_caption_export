[CmdletBinding()]
param([switch]$CheckOnly, [switch]$NoLaunch)

$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$uvVersion = '0.12.7'
$uvArchive = 'uv-x86_64-pc-windows-msvc.zip'
# SHA256 published on https://github.com/astral-sh/uv/releases/tag/0.12.7
$uvSha256 = 'bf1518af459a3915511a11fdc6e2f43ef9a2afa138b9d498eeb9642fe9d85218'
$transcriptStarted = $false
$exitCode = 0

function Invoke-Uv {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$UvArguments)
    & $script:uvPath @UvArguments
    if ($LASTEXITCODE -ne 0) { throw "uv failed (exit $LASTEXITCODE). See the output above." }
}

function Get-VerifiedArchive {
    param([string]$Destination)
    if ((Test-Path -LiteralPath $Destination) -and
        ((Get-FileHash -LiteralPath $Destination -Algorithm SHA256).Hash -eq $uvSha256)) {
        return
    }
    $url = "https://github.com/astral-sh/uv/releases/download/$uvVersion/$uvArchive"
    $part = "$Destination.part"
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        try {
            Write-Host "Downloading uv $uvVersion (attempt $attempt/3)..."
            Invoke-WebRequest -Uri $url -OutFile $part -UseBasicParsing -TimeoutSec 120
            if ((Get-FileHash -LiteralPath $part -Algorithm SHA256).Hash -ne $uvSha256) {
                throw 'Downloaded uv archive failed SHA256 verification.'
            }
            Move-Item -LiteralPath $part -Destination $Destination -Force
            return
        } catch {
            if ($attempt -eq 3) { throw }
            Start-Sleep -Seconds 2
        }
    }
}

try {
    $nativeArch = $env:PROCESSOR_ARCHITECTURE
    if ($env:PROCESSOR_ARCHITEW6432) { $nativeArch = $env:PROCESSOR_ARCHITEW6432 }
    if (-not [Environment]::Is64BitProcess -or $nativeArch -ne 'AMD64') {
        throw 'This installer requires 64-bit Windows on an x64 CPU. Windows ARM and 32-bit Windows are not supported.'
    }
    if ([Environment]::OSVersion.Version.Build -lt 10240) {
        throw 'Windows 10 or newer is required.'
    }
    if ($PSVersionTable.PSVersion.Major -lt 5) { throw 'PowerShell 5.1 or newer is required.' }
    foreach ($file in @('pyproject.toml', 'uv.lock', 'start.py')) {
        if (-not (Test-Path -LiteralPath (Join-Path $projectRoot $file))) {
            throw "Missing $file. Extract the complete source ZIP before running the installer."
        }
    }
    $venvDir = Join-Path $projectRoot '.venv'
    $venvPython = Join-Path $venvDir 'Scripts\python.exe'
    if (Test-Path -LiteralPath $venvDir) {
        if (-not (Test-Path -LiteralPath $venvPython)) {
            throw 'An incomplete .venv already exists. It was preserved. Extract to a new directory, or manually inspect/move the old environment.'
        }
        & $venvPython -I -S -c "import struct,sys; sys.exit(0 if sys.version_info[:2] == (3,12) and struct.calcsize('P') == 8 else 1)"
        if ($LASTEXITCODE -ne 0) {
            throw 'The existing .venv is not a working 64-bit Python 3.12 environment. It was preserved. Use a new extraction directory.'
        }
        Write-Host 'Existing Python 3.12 environment will be reused; extra installed packages will be kept.'
    }
    Write-Host "Platform check passed: Windows x64. Project: $projectRoot"
    if ($CheckOnly) {
        Write-Host 'Check only: no downloads, installation, or application launch performed.'
        exit 0
    }

    $logDir = Join-Path $projectRoot '.cache\install'
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    $logPath = Join-Path $logDir ("install-" + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.log')
    Start-Transcript -LiteralPath $logPath | Out-Null
    $transcriptStarted = $true
    Write-Host "Installation log: $logPath"
    Set-Location -LiteralPath $projectRoot
    $env:PYTHONUTF8 = '1'
    $env:UV_CACHE_DIR = Join-Path $projectRoot '.cache\uv'
    $env:UV_PYTHON_INSTALL_DIR = Join-Path $projectRoot '.runtime\python'
    $env:UV_PROJECT_ENVIRONMENT = $venvDir
    $env:UV_PYTHON_INSTALL_BIN = '0'
    $env:UV_PYTHON_INSTALL_REGISTRY = '0'
    $uvDir = Join-Path $projectRoot ".runtime\uv\$uvVersion"
    New-Item -ItemType Directory -Path $uvDir -Force | Out-Null
    $archivePath = Join-Path $uvDir $uvArchive
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Get-VerifiedArchive -Destination $archivePath
    $script:uvPath = Join-Path $uvDir 'uv.exe'
    if (-not (Test-Path -LiteralPath $script:uvPath)) {
        Expand-Archive -LiteralPath $archivePath -DestinationPath $uvDir -Force
    }
    Invoke-Uv --version

    if (-not (Test-Path -LiteralPath $venvDir)) {
        Invoke-Uv python install 3.12 --no-bin --no-registry --no-config
        Invoke-Uv venv $venvDir --python 3.12 --managed-python --no-config
    }
    Invoke-Uv sync --locked --inexact --no-install-project --no-dev --no-build --no-config --no-python-downloads --python $venvPython --project $projectRoot
    Write-Host 'Installation complete. Model files are downloaded only when a local model is selected and needed.'
    Stop-Transcript | Out-Null
    $transcriptStarted = $false
    if (-not $NoLaunch) {
        & $venvPython (Join-Path $projectRoot 'start.py')
        if ($LASTEXITCODE -ne 0) { throw "Application exited with code $LASTEXITCODE." }
    }
} catch {
    Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
    if ($logPath) { Write-Host "See installation log: $logPath" }
    $exitCode = 1
} finally {
    if ($transcriptStarted) { Stop-Transcript | Out-Null }
}
exit $exitCode
