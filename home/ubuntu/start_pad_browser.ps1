param(
    [ValidateSet("Auto", "Chrome", "Edge")]
    [string]$PreferredBrowser = "Auto",
    [string]$ErrorRoot = "D:\CustomerDeliveryBundle\CustomerPackage\runtime\RPA_Error",
    [string]$TargetUrl = "https://flora2.moenv.gov.tw/MainSite/Lin/index.aspx#gsc.tab=0",
    [int]$CdpPort = 9222
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Get-AbsolutePath {
    param([string]$Path)
    return [System.IO.Path]::GetFullPath($Path)
}

function Test-PathWithin {
    param([string]$Path, [string]$Root)
    $fullPath = (Get-AbsolutePath $Path).TrimEnd('\') + '\'
    $fullRoot = (Get-AbsolutePath $Root).TrimEnd('\') + '\'
    return $fullPath.StartsWith($fullRoot, [System.StringComparison]::OrdinalIgnoreCase)
}

function Copy-Bytes {
    param([byte[]]$Bytes, [int]$Offset, [int]$Length)
    if ($Offset -lt 0 -or $Length -lt 0 -or $Offset + $Length -gt $Bytes.Length) {
        throw "Invalid byte range."
    }
    $result = New-Object byte[] $Length
    [Array]::Copy($Bytes, $Offset, $result, 0, $Length)
    return $result
}

function Read-VarInt {
    param([byte[]]$Bytes, [ref]$Offset, [int]$Limit)
    [UInt64]$value = 0
    $shift = 0
    while ($Offset.Value -lt $Limit) {
        $current = $Bytes[$Offset.Value]
        $Offset.Value++
        $value = $value -bor ([UInt64]($current -band 0x7f) -shl $shift)
        if (($current -band 0x80) -eq 0) { return $value }
        $shift += 7
        if ($shift -ge 64) { throw "Invalid protobuf varint." }
    }
    throw "Unexpected end of protobuf data."
}

function Skip-ProtoField {
    param([byte[]]$Bytes, [int]$WireType, [ref]$Offset, [int]$Limit)
    switch ($WireType) {
        0 { $null = Read-VarInt $Bytes $Offset $Limit }
        1 { $Offset.Value += 8 }
        2 {
            $length = [int](Read-VarInt $Bytes $Offset $Limit)
            $Offset.Value += $length
        }
        5 { $Offset.Value += 4 }
        default { throw "Unsupported protobuf wire type: $WireType" }
    }
    if ($Offset.Value -gt $Limit) { throw "Invalid protobuf field length." }
}

function Get-ProofPublicKey {
    param([byte[]]$ProofBytes)
    $offset = 0
    while ($offset -lt $ProofBytes.Length) {
        $offsetRef = [ref]$offset
        $tag = Read-VarInt $ProofBytes $offsetRef $ProofBytes.Length
        $field = [int]($tag -shr 3)
        $wire = [int]($tag -band 7)
        if ($field -eq 1 -and $wire -eq 2) {
            $length = [int](Read-VarInt $ProofBytes $offsetRef $ProofBytes.Length)
            return Copy-Bytes $ProofBytes $offset $length
        }
        Skip-ProtoField $ProofBytes $wire $offsetRef $ProofBytes.Length
    }
    return $null
}

function Get-Crx3PublicKeys {
    param([byte[]]$HeaderBytes)
    $keys = @()
    $offset = 0
    while ($offset -lt $HeaderBytes.Length) {
        $offsetRef = [ref]$offset
        $tag = Read-VarInt $HeaderBytes $offsetRef $HeaderBytes.Length
        $field = [int]($tag -shr 3)
        $wire = [int]($tag -band 7)
        if ($wire -eq 2 -and ($field -eq 2 -or $field -eq 3)) {
            $length = [int](Read-VarInt $HeaderBytes $offsetRef $HeaderBytes.Length)
            $proof = Copy-Bytes $HeaderBytes $offset $length
            $offset += $length
            $key = Get-ProofPublicKey $proof
            if ($key) { $keys += ,$key }
            continue
        }
        Skip-ProtoField $HeaderBytes $wire $offsetRef $HeaderBytes.Length
    }
    return @($keys)
}

function Convert-ToExtensionId {
    param([byte[]]$PublicKey)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try { $hash = $sha.ComputeHash($PublicKey) } finally { $sha.Dispose() }
    $alphabet = "abcdefghijklmnop"
    $builder = [System.Text.StringBuilder]::new()
    for ($i = 0; $i -lt 16; $i++) {
        [void]$builder.Append($alphabet[($hash[$i] -shr 4) -band 15])
        [void]$builder.Append($alphabet[$hash[$i] -band 15])
    }
    return $builder.ToString()
}

function Get-ManifestExtensionId {
    param([string]$ManifestPath)
    $manifest = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
    if (-not ($manifest.PSObject.Properties.Name -contains "key")) { return $null }
    if ([string]::IsNullOrWhiteSpace([string]$manifest.key)) { return $null }
    return Convert-ToExtensionId ([Convert]::FromBase64String([string]$manifest.key))
}

function Set-ManifestKey {
    param([string]$ManifestPath, [byte[]]$PublicKey)
    $manifest = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
    $manifest | Add-Member -MemberType NoteProperty -Name key -Value ([Convert]::ToBase64String($PublicKey)) -Force
    $utf8 = [System.Text.UTF8Encoding]::new($false)
    [System.IO.File]::WriteAllText($ManifestPath, ($manifest | ConvertTo-Json -Depth 100), $utf8)
}

function Expand-PadCrx {
    param([string]$CrxPath, [string]$DestinationPath, [string[]]$AllowedIds)
    $bytes = [System.IO.File]::ReadAllBytes($CrxPath)
    if ($bytes.Length -lt 16 -or [Text.Encoding]::ASCII.GetString($bytes, 0, 4) -ne "Cr24") {
        throw "Invalid PAD CRX package: $CrxPath"
    }
    $version = [BitConverter]::ToInt32($bytes, 4)
    $selectedKey = $null
    if ($version -eq 2) {
        $keyLength = [BitConverter]::ToInt32($bytes, 8)
        $signatureLength = [BitConverter]::ToInt32($bytes, 12)
        $selectedKey = Copy-Bytes $bytes 16 $keyLength
        $zipStart = 16 + $keyLength + $signatureLength
        $id = Convert-ToExtensionId $selectedKey
        if ($AllowedIds -notcontains $id) { throw "CRX2 Extension ID is not allowed: $id" }
    }
    elseif ($version -eq 3) {
        $headerLength = [BitConverter]::ToInt32($bytes, 8)
        $headerStart = 12
        $headerEnd = $headerStart + $headerLength
        if ($headerLength -le 0 -or $headerEnd -ge $bytes.Length) { throw "Invalid CRX3 header." }
        $header = Copy-Bytes $bytes $headerStart $headerLength
        $candidates = @(Get-Crx3PublicKeys $header | ForEach-Object {
            [PSCustomObject]@{ Id = Convert-ToExtensionId $_; Key = $_ }
        })
        $matches = @($candidates | Where-Object { $AllowedIds -contains $_.Id })
        if ($matches.Count -ne 1) {
            $candidateIds = ($candidates.Id -join ", ")
            throw "CRX3 key cannot be resolved safely. Candidates: $candidateIds"
        }
        $selectedKey = $matches[0].Key
        $zipStart = $headerEnd
    }
    else { throw "Unsupported CRX version: $version" }

    if ($zipStart -ge $bytes.Length) { throw "CRX ZIP payload is missing." }
    $zipPath = [IO.Path]::ChangeExtension([IO.Path]::GetTempFileName(), ".zip")
    try {
        [IO.File]::WriteAllBytes($zipPath, (Copy-Bytes $bytes $zipStart ($bytes.Length - $zipStart)))
        if (Test-Path -LiteralPath $DestinationPath) {
            Remove-Item -LiteralPath $DestinationPath -Recurse -Force
        }
        New-Item -ItemType Directory -Path $DestinationPath -Force | Out-Null
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        [IO.Compression.ZipFile]::ExtractToDirectory($zipPath, $DestinationPath)
    }
    finally { Remove-Item -LiteralPath $zipPath -Force -ErrorAction SilentlyContinue }

    $manifestPath = Join-Path $DestinationPath "manifest.json"
    Set-ManifestKey $manifestPath $selectedKey
    return Get-ManifestExtensionId $manifestPath
}

function Get-ProcessSnapshot {
    param([string]$Name)
    return @(Get-CimInstance Win32_Process -Filter "Name = '$Name'" -ErrorAction SilentlyContinue)
}

function Get-CdpOwners {
    return @(Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $CdpPort -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique)
}

function Get-NativeManifest {
    param([string[]]$RegistryPaths)
    foreach ($registryPath in $RegistryPaths) {
        if (-not (Test-Path -LiteralPath $registryPath)) { continue }
        $value = (Get-Item -LiteralPath $registryPath).GetValue("")
        if ($value) {
            $expanded = [Environment]::ExpandEnvironmentVariables([string]$value)
            if (Test-Path -LiteralPath $expanded) { return $expanded }
        }
    }
    return $null
}

$browserDefinitions = @{
    Chrome = @{
        ProcessName = "chrome.exe"
        Executables = @("C:\Program Files\Google\Chrome\Application\chrome.exe", "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe")
        CrxPattern = "*chrome*.crx"
        Profile = Join-Path $ErrorRoot "chrome-cdp"
        Extension = Join-Path $ErrorRoot "pad-extension-chrome"
        Registry = @("HKCU:\SOFTWARE\Google\Chrome\NativeMessagingHosts\com.microsoft.pad.messagehost", "HKLM:\SOFTWARE\Google\Chrome\NativeMessagingHosts\com.microsoft.pad.messagehost", "HKLM:\SOFTWARE\WOW6432Node\Google\Chrome\NativeMessagingHosts\com.microsoft.pad.messagehost")
    }
    Edge = @{
        ProcessName = "msedge.exe"
        Executables = @("C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", "C:\Program Files\Microsoft\Edge\Application\msedge.exe")
        CrxPattern = "*edge*.crx"
        Profile = Join-Path $ErrorRoot "edge-cdp"
        Extension = Join-Path $ErrorRoot "pad-extension-edge"
        Registry = @("HKCU:\SOFTWARE\Microsoft\Edge\NativeMessagingHosts\com.microsoft.pad.messagehost", "HKLM:\SOFTWARE\Microsoft\Edge\NativeMessagingHosts\com.microsoft.pad.messagehost", "HKLM:\SOFTWARE\WOW6432Node\Microsoft\Edge\NativeMessagingHosts\com.microsoft.pad.messagehost")
    }
}

New-Item -ItemType Directory -Path $ErrorRoot -Force | Out-Null
$browserName = $null
if ($PreferredBrowser -eq "Auto") {
    foreach ($candidate in @("Chrome", "Edge")) {
        if ($browserDefinitions[$candidate].Executables | Where-Object { Test-Path -LiteralPath $_ }) { $browserName = $candidate; break }
    }
}
else { $browserName = $PreferredBrowser }
if (-not $browserName) { throw "No supported browser was found." }
$config = $browserDefinitions[$browserName]
$browserPath = $config.Executables | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $browserPath) { throw "$browserName executable was not found." }
$profilePath = Get-AbsolutePath $config.Profile
$extensionPath = Get-AbsolutePath $config.Extension
if (-not (Test-PathWithin $profilePath $ErrorRoot) -or -not (Test-PathWithin $extensionPath $ErrorRoot)) { throw "Refusing to use a path outside ErrorRoot." }

$nativeManifestPath = Get-NativeManifest $config.Registry
if (-not $nativeManifestPath) { throw "Native Messaging manifest was not found for $browserName." }
$nativeManifest = Get-Content -LiteralPath $nativeManifestPath -Raw | ConvertFrom-Json
$allowedIds = @($nativeManifest.allowed_origins | ForEach-Object { if ([string]$_ -match '://([a-p]{32})/') { $Matches[1].ToLowerInvariant() } } | Sort-Object -Unique)
if (-not $allowedIds) { throw "Native Messaging manifest has no allowed extension IDs." }

$extensionRoots = @(
    "C:\Program Files (x86)\Power Automate Desktop\BrowserExtensions",
    "C:\Program Files\Power Automate Desktop\BrowserExtensions"
)
$extensionRoot = $extensionRoots | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $extensionRoot) { throw "PAD BrowserExtensions directory was not found." }
$crx = Get-ChildItem -LiteralPath $extensionRoot -Filter $config.CrxPattern -File | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $crx) { throw "PAD CRX package was not found under $extensionRoot." }
$manifestPath = Join-Path $extensionPath "manifest.json"
$hashMarker = Join-Path $extensionPath ".source-crx.sha256"
$sourceHash = (Get-FileHash -LiteralPath $crx.FullName -Algorithm SHA256).Hash
$needExtract = $true
if ((Test-Path $manifestPath) -and (Test-Path $hashMarker)) {
    $cachedId = Get-ManifestExtensionId $manifestPath
    $cachedHash = (Get-Content -LiteralPath $hashMarker -Raw).Trim()
    if ($cachedHash -eq $sourceHash -and $allowedIds -contains $cachedId) { $needExtract = $false }
}
if ($needExtract) {
    $extensionId = Expand-PadCrx $crx.FullName $extensionPath $allowedIds
    [IO.File]::WriteAllText($hashMarker, $sourceHash, [Text.UTF8Encoding]::new($false))
}
$extensionId = Get-ManifestExtensionId $manifestPath
if ($allowedIds -notcontains $extensionId) { throw "Extension ID is not allowed: $extensionId" }

$targetProcesses = @(Get-ProcessSnapshot $config.ProcessName | Where-Object { $_.CommandLine -and $_.CommandLine -like "*$profilePath*" })
foreach ($process in $targetProcesses) { Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Milliseconds 500

$portOwners = @(Get-CdpOwners)
if ($portOwners.Count -gt 0) {
    $details = foreach ($ownerPid in $portOwners) { Get-CimInstance Win32_Process -Filter "ProcessId = $ownerPid" | Select-Object ProcessId, CommandLine }
    throw "CDP port $CdpPort is already owned by another process: $($details | Out-String)"
}

$arguments = @(
    "--remote-debugging-port=$CdpPort",
    "--user-data-dir=`"$profilePath`"",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-session-crashed-bubble",
    "--load-extension=`"$extensionPath`"",
    $TargetUrl
)
$browserProcess = Start-Process -FilePath $browserPath -ArgumentList $arguments -WindowStyle Normal -PassThru

$cdpReady = $false
for ($attempt = 1; $attempt -le 180; $attempt++) {
    try {
        $version = Invoke-RestMethod -Uri "http://127.0.0.1:$CdpPort/json/version" -TimeoutSec 2
        if ($version.webSocketDebuggerUrl) { $cdpReady = $true; break }
    }
    catch { }
    Start-Sleep -Seconds 1
}
if (-not $cdpReady) { throw "$browserName CDP port $CdpPort was not ready within 180 seconds." }

$profileProcesses = @(Get-ProcessSnapshot $config.ProcessName | Where-Object { $_.CommandLine -and $_.CommandLine -like "*$profilePath*" })
if ($profileProcesses.Count -eq 0) { throw "CDP is open, but no $browserName process belongs to profile $profilePath." }

$statePath = Join-Path $ErrorRoot "pad-browser-state.json"
$state = [ordered]@{
    Browser = $browserName
    BrowserPath = $browserPath
    BrowserPid = $browserProcess.Id
    Profile = $profilePath
    ExtensionPath = $extensionPath
    ExtensionId = $extensionId
    CdpPort = $CdpPort
    TargetUrl = $TargetUrl
    StartedAt = (Get-Date).ToString("o")
}
$state | ConvertTo-Json | Set-Content -LiteralPath $statePath -Encoding UTF8

Write-Output "Browser ready: $browserName"
Write-Output "Profile: $profilePath"
Write-Output "Extension ID: $extensionId"
Write-Output "CDP: http://127.0.0.1:$CdpPort"
Write-Output "State: $statePath"
