param(
    [string]$ContextFilePath = "",
    [string]$ErrorImagePath = "",
    [string]$BackendMode = $env:SELF_HEAL_BACKEND,
    [string]$LegacyAgentScriptPath = "",
    [string]$VenvPath = "",
    [string]$OpenAIApiKey = $env:OPENAI_API_KEY
)

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$scriptRoot = $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($scriptRoot)) {
    $scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
}

$repoRoot = Split-Path -Parent (Split-Path -Parent $scriptRoot)
$runtimeRoot = Join-Path $repoRoot ".runtime"

function Resolve-PortablePath {
    param(
        [string]$Path,
        [string]$BasePath
    )

    if ([string]::IsNullOrWhiteSpace($Path)) {
        return ""
    }

    if ([System.IO.Path]::IsPathRooted($Path)) {
        return $Path
    }

    if ([string]::IsNullOrWhiteSpace($BasePath)) {
        return $Path
    }

    return (Join-Path $BasePath $Path)
}

if ([string]::IsNullOrWhiteSpace($ContextFilePath)) {
    $ContextFilePath = Join-Path $runtimeRoot "RPA_Error\context.json"
}
if ([string]::IsNullOrWhiteSpace($ErrorImagePath)) {
    $ErrorImagePath = Join-Path $runtimeRoot "RPA_Error\ERROR.jpg"
}
if ([string]::IsNullOrWhiteSpace($LegacyAgentScriptPath)) {
    $LegacyAgentScriptPath = Join-Path $scriptRoot "webwright_agent.py"
}
if ([string]::IsNullOrWhiteSpace($VenvPath)) {
    $VenvPath = Join-Path $repoRoot ".venv"
}

$defaultAiBaseUrl = "http://172.22.8.15:8080"
# This customer bundle is fixed to the internal AI gateway. Do not allow
# inherited AI_API_BASE_URL/OPENAI_BASE_URL values to redirect requests externally.
$configuredAiBaseUrl = $defaultAiBaseUrl

$ContextFilePath = Resolve-PortablePath -Path $ContextFilePath -BasePath $repoRoot
$ErrorImagePath = Resolve-PortablePath -Path $ErrorImagePath -BasePath $repoRoot
$LegacyAgentScriptPath = Resolve-PortablePath -Path $LegacyAgentScriptPath -BasePath $repoRoot
$VenvPath = Resolve-PortablePath -Path $VenvPath -BasePath $repoRoot

$env:AI_ARTIFACT_DIR = $runtimeRoot
$env:AI_BRIDGE_LOG_PATH = Join-Path $runtimeRoot "bridge.log"
$env:AI_API_BASE_URL = $configuredAiBaseUrl
$env:OPENAI_BASE_URL = $configuredAiBaseUrl
$env:OPENAI_BASE = $configuredAiBaseUrl
$env:AI_ALLOW_OPENAI_FALLBACK = "false"

function Get-SecretFingerprint {
    param([string]$Value)

    if ([string]::IsNullOrWhiteSpace($Value)) {
        return ""
    }

    $bytes = [System.Text.Encoding]::UTF8.GetBytes($Value)
    $hash = [System.Security.Cryptography.SHA256]::Create().ComputeHash($bytes)
    return ([System.BitConverter]::ToString($hash) -replace "-", "").Substring(0, 12)
}

$openAiKeyFingerprint = Get-SecretFingerprint -Value $OpenAIApiKey

function Write-LauncherLog {
    param([string]$Message)
    try {
        $logPath = Join-Path $runtimeRoot "launcher.log"
        $ts = [DateTime]::UtcNow.ToString("o")
        "[$ts] $Message" | Out-File -FilePath $logPath -Append -Encoding UTF8
    } catch {}
}

function Resolve-AgentScriptPath {
    param(
        [string]$LegacyAgentScriptPath
    )

    $portableFallback = Join-Path $scriptRoot "webwright_agent.py"
    if (-not (Test-Path $LegacyAgentScriptPath) -and (Test-Path $portableFallback)) {
        $LegacyAgentScriptPath = $portableFallback
    }

    return $LegacyAgentScriptPath
}

function Test-CdpReady {
    param(
        [int]$Port = 9222
    )

    try {
        # TCP port 開啟不代表 CDP HTTP endpoint 已完成啟動；確認
        # /json/version 有效且包含瀏覽器 WebSocket 位址再交給 Python。
        $version = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/json/version" -TimeoutSec 2
        $version = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/json/version" -TimeoutSec 2
        return (-not [string]::IsNullOrWhiteSpace([string]$version.webSocketDebuggerUrl))
    }
    catch {
        return $false
    }
}

function Wait-ForCdpReady {
    param(
        [int]$Port = 9222,
        [int]$TimeoutSeconds = 20
    )

    for ($attempt = 1; $attempt -le $TimeoutSeconds; $attempt++) {
        if (Test-CdpReady -Port $Port) {
            return $true
        }

        Start-Sleep -Seconds 1
    }

    return $false
}

if (-not (Test-Path $ContextFilePath)) {
    $contextDir = Split-Path -Parent $ContextFilePath
    if (-not [string]::IsNullOrWhiteSpace($contextDir) -and -not (Test-Path $contextDir)) {
        New-Item -ItemType Directory -Path $contextDir -Force | Out-Null
    }
}

$AgentScriptPath = Resolve-AgentScriptPath -LegacyAgentScriptPath $LegacyAgentScriptPath
$resolvedBackend = "legacy"

if (-not (Test-Path $AgentScriptPath)) {
    Write-LauncherLog "webwright_start result=missing_agent_script path=$AgentScriptPath backendMode=$BackendMode resolvedBackend=$resolvedBackend"
    Write-Error "Missing agent script: $AgentScriptPath"
    exit 1
}

if (-not (Test-Path $VenvPath)) {
    Write-LauncherLog "webwright_start result=missing_venv path=$VenvPath"
    Write-Error "Missing virtual environment: $VenvPath"
    exit 1
}

$pythonExecutable = Join-Path $VenvPath "Scripts\python.exe"
if (-not (Test-Path $pythonExecutable)) {
    Write-LauncherLog "webwright_start result=missing_python path=$pythonExecutable"
    Write-Error "Missing Python executable: $pythonExecutable"
    exit 1
}

$contextDir = Split-Path -Parent $ContextFilePath
if (-not (Test-Path $contextDir)) {
    New-Item -ItemType Directory -Path $contextDir -Force | Out-Null
}

if (-not (Test-Path $ErrorImagePath)) {
    Write-LauncherLog "webwright_start result=missing_error_image path=$ErrorImagePath"
    Write-Error "Missing error image: $ErrorImagePath"
    exit 1
}

if ([string]::IsNullOrWhiteSpace($OpenAIApiKey)) {
    $OpenAIApiKey = $env:OPENAI_API_KEY
}
if ([string]::IsNullOrWhiteSpace($OpenAIApiKey)) {
    throw "OPENAI_API_KEY is required."
}

$allowedHosts = @("localhost", "127.0.0.1")
$targetUrl = ""
try {
    if (Test-Path $ContextFilePath) {
        $contextPreview = Get-Content -LiteralPath $ContextFilePath -Raw -Encoding UTF8
        if (-not [string]::IsNullOrWhiteSpace($contextPreview)) {
            $contextObject = $contextPreview | ConvertFrom-Json
            if ($contextObject -and $contextObject.CurrentURL) {
                $uri = [Uri]$contextObject.CurrentURL
                if ($uri.Host) {
                    $allowedHosts += $uri.Host
                }
                if (-not [string]::IsNullOrWhiteSpace($uri.AbsoluteUri)) {
                    $targetUrl = $uri.AbsoluteUri
                }
            }
        }
    }
} catch {
    Write-LauncherLog "webwright_start result=allowed_hosts_context_parse_failed error=$($_.Exception.Message)"
}

$env:OPENAI_API_KEY = $OpenAIApiKey
if ([string]::IsNullOrWhiteSpace($env:AI_MODEL)) {
    $env:AI_MODEL = "gpt-5.4-nano"
}
$env:OPENAI_BASE_URL = $configuredAiBaseUrl
$env:OPENAI_API_BASE = $configuredAiBaseUrl
$env:OPENAI_COMPATIBLE_API_BASE = $configuredAiBaseUrl

$noProxyHosts = @("localhost", "127.0.0.1")
if (-not [string]::IsNullOrWhiteSpace($env:AI_API_BASE_URL)) {
    try {
        $uri = [Uri]$env:AI_API_BASE_URL
        if ($uri.Host) {
            $noProxyHosts += $uri.Host
        }
    } catch {
    }
}

$allowedHosts += $noProxyHosts
$noProxyValue = ($noProxyHosts | Where-Object { $_ } | Select-Object -Unique) -join ","
if ([string]::IsNullOrWhiteSpace($env:NO_PROXY)) {
    $env:NO_PROXY = $noProxyValue
} else {
    $env:NO_PROXY = (($env:NO_PROXY.Split(",") + $noProxyHosts) | Where-Object { $_ } | Select-Object -Unique) -join ","
}
$env:no_proxy = $env:NO_PROXY
$env:ALLOWED_HOSTS = (($allowedHosts | Where-Object { $_ } | Select-Object -Unique) | ConvertTo-Json -Compress)
$env:HTTP_PROXY = ""
$env:HTTPS_PROXY = ""
$env:ALL_PROXY = ""
$env:http_proxy = ""
$env:https_proxy = ""
$env:all_proxy = ""
Write-LauncherLog "webwright_start result=begin backendMode=$BackendMode resolvedBackend=$resolvedBackend agentScript=$AgentScriptPath contextFile=$ContextFilePath errorImage=$ErrorImagePath python=$pythonExecutable aiBaseUrl=$configuredAiBaseUrl openAiKeyFingerprint=$openAiKeyFingerprint"

try {
    $cdpProbe = Test-CdpReady -Port 9222
    $browserWindow = @(
        Get-Process msedge, chrome -ErrorAction SilentlyContinue |
        Where-Object { $_.MainWindowHandle -ne 0 }
    )
    $cdpReady = ($cdpProbe -and $browserWindow.Count -gt 0)
    if ($cdpReady) {
        Write-LauncherLog "webwright_start result=cdp_ready_visible_window=true port=9222 targetUrl=$targetUrl"
    }
    elseif ($cdpProbe) {
        Write-LauncherLog "webwright_start result=cdp_ready=false port_open_no_visible_window_or_browser_process port=9222 targetUrl=$targetUrl"
    }
}
catch {
    $cdpReady = $false
    Write-LauncherLog "webwright_start result=cdp_probe_error error=$($_.Exception.Message) port=9222 targetUrl=$targetUrl"
}

if (-not $cdpReady) {
    Write-LauncherLog "webwright_start result=cdp_wait_existing_start port=9222 targetUrl=$targetUrl"
    if (-not (Wait-ForCdpReady -Port 9222 -TimeoutSeconds 20)) {
        Write-LauncherLog "webwright_start result=cdp_wait_existing_timeout port=9222 targetUrl=$targetUrl"
        Write-Error "CDP port 9222 is not ready on the existing Chrome/Edge window."
        exit 1
    }

    Write-LauncherLog "webwright_start result=cdp_wait_existing_ready port=9222 targetUrl=$targetUrl"
}

$pythonStdOutPath = Join-Path $contextDir "python_stdout.log"
$pythonStdErrPath = Join-Path $contextDir "python_stderr.log"

& $pythonExecutable $AgentScriptPath --context $ContextFilePath --img $ErrorImagePath 1> $pythonStdOutPath 2> $pythonStdErrPath
Write-LauncherLog "webwright_start result=python_exit exitCode=$LASTEXITCODE stdoutLog=$pythonStdOutPath stderrLog=$pythonStdErrPath"

if ($LASTEXITCODE -ne 0) {
    $stdoutTail = ""
    $stderrTail = ""

    if (Test-Path $pythonStdOutPath) {
        $stdoutTail = (Get-Content -Raw -Encoding UTF8 $pythonStdOutPath).Trim()
    }

    if (Test-Path $pythonStdErrPath) {
        $stderrTail = (Get-Content -Raw -Encoding UTF8 $pythonStdErrPath).Trim()
    }

    if ([string]::IsNullOrWhiteSpace($stderrTail)) {
        Write-LauncherLog "webwright_start result=failed exitCode=$LASTEXITCODE stdoutTail=$stdoutTail"
        Write-Error "Webwright Agent failed. Exit Code: $LASTEXITCODE. StdOut: $stdoutTail"
    }
    else {
        Write-LauncherLog "webwright_start result=failed exitCode=$LASTEXITCODE stderrTail=$stderrTail"
        Write-Error "Webwright Agent failed. Exit Code: $LASTEXITCODE. StdErr: $stderrTail"
    }

    exit $LASTEXITCODE
}

Write-Host "Webwright Agent completed successfully."
exit 0
