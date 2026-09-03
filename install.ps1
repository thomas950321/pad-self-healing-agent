param(
    [switch]$ForceRecreateVenv
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$scriptRoot = $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($scriptRoot)) {
    $scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
}

Set-Location $scriptRoot

$venvPath = Join-Path $scriptRoot ".venv"
$requirementsPath = Join-Path $scriptRoot "requirements.txt"
$runtimePath = Join-Path $scriptRoot ".runtime"
$runtimeErrorPath = Join-Path $runtimePath "RPA_Error"

function Write-InstallLog {
    param([string]$Message)
    Write-Host "[install] $Message"
}

function Resolve-CommandExecutable {
    param($CommandInfo)
    if ($null -eq $CommandInfo) {
        return ""
    }

    if ($CommandInfo.Source) {
        return [string]$CommandInfo.Source
    }

    if ($CommandInfo.Path) {
        return [string]$CommandInfo.Path
    }

    return [string]$CommandInfo.Name
}

function Test-PythonExecutable {
    param(
        [string]$Executable,
        [string[]]$Arguments = @()
    )

    if ([string]::IsNullOrWhiteSpace($Executable)) {
        return $false
    }

    try {
        & $Executable @Arguments --version *> $null
        return ($LASTEXITCODE -eq 0)
    }
    catch {
        return $false
    }
}

function Get-PythonBootstrap {
    $pyCommand = Get-Command py -ErrorAction SilentlyContinue
    if ($pyCommand) {
        $pyExe = Resolve-CommandExecutable $pyCommand
        if (Test-PythonExecutable -Executable $pyExe -Arguments @("-3")) {
            return @{
                Exe = $pyExe
                Args = @("-3")
            }
        }
    }

    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCommand) {
        $pythonExe = Resolve-CommandExecutable $pythonCommand
        if (Test-PythonExecutable -Executable $pythonExe) {
            return @{
                Exe = $pythonExe
                Args = @()
            }
        }
    }

    return $null
}

function Test-CandidatePythonPath {
    param([string]$Path)

    if ([string]::IsNullOrWhiteSpace($Path) -or -not (Test-Path -LiteralPath $Path)) {
        return $false
    }

    return Test-PythonExecutable -Executable $Path
}

function Find-PythonAfterInstall {
    $knownPaths = @(
        (Join-Path $env:LocalAppData "Programs\Python\Python311\python.exe"),
        (Join-Path $env:LocalAppData "Programs\Python\Python312\python.exe"),
        (Join-Path $env:ProgramFiles "Python311\python.exe"),
        (Join-Path $env:ProgramFiles "Python312\python.exe")
    ) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }

    foreach ($candidate in $knownPaths) {
        if (Test-CandidatePythonPath -Path $candidate) {
            return $candidate
        }
    }

    return $null
}

function Install-PythonWithWinget {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw "Python was not found and winget is not available. Please install Python 3.11 or newer and run this script again."
    }

    Write-InstallLog "Python was not found. Installing Python 3.11 with winget."
    $wingetArgs = @(
        "install",
        "--id", "Python.Python.3.11",
        "-e",
        "--scope", "user",
        "--accept-package-agreements",
        "--accept-source-agreements",
        "--disable-interactivity"
    )

    & winget @wingetArgs
    if ($LASTEXITCODE -ne 0) {
        throw "winget failed to install Python. Exit code: $LASTEXITCODE"
    }
}

function Ensure-PythonAvailable {
    $bootstrap = Get-PythonBootstrap
    if ($bootstrap) {
        return @{
            Exe = $bootstrap.Exe
            Args = $bootstrap.Args
        }
    }

    Install-PythonWithWinget

    Start-Sleep -Seconds 2
    $bootstrap = Get-PythonBootstrap
    if ($bootstrap) {
        return $bootstrap
    }

    $pythonPath = Find-PythonAfterInstall
    if ($pythonPath) {
        return @{
            Exe = $pythonPath
            Args = @()
        }
    }

    throw "Python installation completed, but this shell could not locate the executable yet. Close and reopen PowerShell, then run install.ps1 again."
}

function Ensure-Directory {
    param([string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) {
        return
    }
    if (-not (Test-Path -LiteralPath $Path)) {
        New-Item -ItemType Directory -Path $Path -Force | Out-Null
    }
}

function Get-WheelhousePath {
    $candidate = Join-Path $scriptRoot "wheelhouse"
    if (-not (Test-Path -LiteralPath $candidate)) {
        return $null
    }

    $wheelCount = @(Get-ChildItem -LiteralPath $candidate -Filter *.whl -File -ErrorAction SilentlyContinue).Count
    if ($wheelCount -gt 0) {
        return $candidate
    }

    return $null
}

# Retry pip because corporate proxies can occasionally reset a single request.
function Invoke-PipInstall {
    param(
        [string[]]$Arguments,
        [string]$FindLinksPath = "",
        [int]$MaxAttempts = 3
    )

    $pipArgs = @(
        "install"
        "--disable-pip-version-check"
    )

    if (-not [string]::IsNullOrWhiteSpace($FindLinksPath)) {
        $pipArgs += @("--no-index", "--find-links", $FindLinksPath)
    }

    $pipArgs += @(
        "--retries", "5"
        "--timeout", "60"
        "--prefer-binary"
        "--progress-bar", "off"
    )
    $pipArgs += $Arguments

    for ($attempt = 1; $attempt -le $MaxAttempts; $attempt++) {
        & $venvPython -m pip @pipArgs
        if ($LASTEXITCODE -eq 0) {
            return
        }

        if ($attempt -lt $MaxAttempts) {
            $delaySeconds = [Math]::Min(15, [Math]::Pow(2, $attempt))
            Write-InstallLog "pip attempt $attempt failed with exit code $LASTEXITCODE. Retrying in $delaySeconds seconds."
            Start-Sleep -Seconds $delaySeconds
        }
    }

    throw "pip failed after $MaxAttempts attempts. Exit code: $LASTEXITCODE"
}

Write-InstallLog "Package root: $scriptRoot"
Ensure-Directory -Path $runtimePath
Ensure-Directory -Path $runtimeErrorPath

if (-not (Test-Path -LiteralPath $requirementsPath)) {
    throw "Missing requirements.txt at $requirementsPath"
}

$python = Ensure-PythonAvailable

$venvPython = Join-Path $venvPath "Scripts\python.exe"
$venvNeedsRecreate = $ForceRecreateVenv
if (Test-Path -LiteralPath $venvPath) {
    if (-not (Test-Path -LiteralPath $venvPython) -or -not (Test-PythonExecutable -Executable $venvPython)) {
        $venvNeedsRecreate = $true
        Write-InstallLog "Existing .venv is not usable. Recreating it."
    }
}

if ($venvNeedsRecreate -and (Test-Path -LiteralPath $venvPath)) {
    Write-InstallLog "Removing existing .venv"
    Remove-Item -LiteralPath $venvPath -Recurse -Force
}

if (-not (Test-Path -LiteralPath $venvPath)) {
    Write-InstallLog "Creating .venv"
    & $python.Exe @($python.Args + @("-m", "venv", $venvPath))
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create .venv. Exit code: $LASTEXITCODE"
    }
}

if (-not (Test-Path -LiteralPath $venvPython)) {
    throw "Missing venv Python executable at $venvPython"
}

Write-InstallLog "Installing requirements"
$wheelhousePath = Get-WheelhousePath
if ($wheelhousePath) {
    Write-InstallLog "Using local wheelhouse at $wheelhousePath"
    Invoke-PipInstall -Arguments @("-r", $requirementsPath) -FindLinksPath $wheelhousePath -MaxAttempts 1
}
else {
    Write-InstallLog "Local wheelhouse not found, installing from PyPI with retries"
    Invoke-PipInstall -Arguments @("-r", $requirementsPath)
}

$installPlaywrightBrowsers = $false
if ($env:INSTALL_PLAYWRIGHT_BROWSERS) {
    $installPlaywrightBrowsers = $env:INSTALL_PLAYWRIGHT_BROWSERS.Trim().ToLowerInvariant() -in @("1", "true", "yes", "on")
}

if ($installPlaywrightBrowsers) {
    Write-InstallLog "Installing Playwright browsers"
    & $venvPython -m playwright install chromium
    if ($LASTEXITCODE -ne 0) {
        Write-InstallLog "Warning: Failed to install Playwright browsers, but CDP connection may still work."
    }
}
else {
    Write-InstallLog "Skipping Playwright browser download; CDP mode does not require local browsers."
}

Write-InstallLog "Done"
Write-Host ""
Write-Host "Next step:"
Write-Host "  Import the required PAD flow from PAD_flows into Power Automate Desktop."
