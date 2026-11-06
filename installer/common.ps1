# Shared helpers for the Get Files Windows installer. Windows PowerShell 5.1 compatible.
# Dot-source this file; it defines functions only.

# Started from a PowerShell 7 window, Windows PowerShell inherits a module path it cannot use.
if ($PSVersionTable.PSEdition -ne 'Core') {
    $env:PSModulePath = @(
        (Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'WindowsPowerShell\Modules'),
        (Join-Path $env:ProgramFiles 'WindowsPowerShell\Modules'),
        (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\Modules')
    ) -join ';'
}

$script:GfMarker = '.get-files-install'

function Write-Step([string]$Text) { Write-Host ''; Write-Host ('== ' + $Text) -ForegroundColor Cyan }
function Write-Info([string]$Text) { Write-Host ('   ' + $Text) }
function Write-Good([string]$Text) { Write-Host ('   OK  ' + $Text) -ForegroundColor Green }
function Write-Warn2([string]$Text) { Write-Host ('   !   ' + $Text) -ForegroundColor Yellow }
function Write-Bad([string]$Text) { Write-Host ('   X   ' + $Text) -ForegroundColor Red }

function Read-YesNo([string]$Question, [bool]$Default = $false) {
    $suffix = if ($Default) { ' [Y/n] ' } else { ' [y/N] ' }
    while ($true) {
        $answer = Read-Host -Prompt ('   ' + $Question + $suffix)
        if ($null -eq $answer) { return $Default }
        $answer = $answer.Trim().ToLowerInvariant()
        if ($answer -eq '') { return $Default }
        if ($answer -in @('y', 'yes')) { return $true }
        if ($answer -in @('n', 'no')) { return $false }
        Write-Info 'Please type Y for yes or N for no.'
    }
}

function Write-Utf8NoBom([string]$Path, [string]$Text) {
    [IO.File]::WriteAllText($Path, $Text, (New-Object Text.UTF8Encoding $false))
}

function Get-GfPaths([string]$Root) {
    return @{
        Root   = $Root
        Marker = Join-Path $Root $script:GfMarker
        Config = Join-Path $Root 'installer.json'
        Venv   = Join-Path $Root '.venv\Scripts\python.exe'
        Data   = Join-Path $Root 'data'
        Token  = Join-Path $Root 'data\service-token.txt'
        Logs   = Join-Path $Root 'logs'
    }
}

function Read-GfConfig([string]$Root) {
    return (Get-Content -LiteralPath (Join-Path $Root 'installer.json') -Raw -Encoding UTF8 | ConvertFrom-Json)
}

function Read-GfToken([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    return ([IO.File]::ReadAllText($Path)).Trim([char]0xFEFF, ' ', "`r", "`n", "`t")
}

function Test-PortFree([int]$Port) {
    $listener = New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback, $Port)
    try { $listener.Start(); return $true } catch { return $false } finally { try { $listener.Stop() } catch {} }
}

function Update-SessionPath {
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = (@($machine, $user) | Where-Object { $_ }) -join ';'
}

# Returns @{ Exe; Version } for Python 3.12+, preferring 3.12 (the version the tests run on).
function Find-GfPython([string]$Requested) {
    # No quote characters: Windows PowerShell 5.1 drops embedded double quotes when calling native programs.
    $probe = 'import sys;print(sys.executable);print(sys.version_info[0],sys.version_info[1],sys.version_info[2],sep=chr(46))'
    $candidates = New-Object Collections.Generic.List[object]
    if ($Requested) { $candidates.Add(@($Requested)) }
    else {
        foreach ($v in @('3.12', '3.13', '3.14')) { $candidates.Add(@('py', "-$v")) }
        $candidates.Add(@('python'))
        foreach ($v in @('312', '313', '314')) {
            foreach ($base in @("$env:LOCALAPPDATA\Programs\Python\Python$v", "$env:ProgramFiles\Python$v", "C:\Python$v")) {
                $candidates.Add(@((Join-Path $base 'python.exe')))
            }
        }
    }
    foreach ($c in $candidates) {
        $exe = $c[0]; $extra = @($c | Select-Object -Skip 1)
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        try {
            $out = & $exe @extra -c $probe 2>$null
            if ($LASTEXITCODE -ne 0 -or -not $out) { continue }
            $lines = @($out)
            if ($lines.Count -lt 2) { continue }
            $ver = [version]$lines[1]
            if ($ver.Major -eq 3 -and $ver.Minor -ge 12 -and $lines[0] -notmatch '\\WindowsApps\\') {
                return @{ Exe = $lines[0].Trim(); Version = $ver }
            }
        } catch { continue }
    }
    return $null
}

function Find-GfNode {
    $node = (Get-Command node -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1)
    $exe = if ($node) { $node.Source } elseif (Test-Path "$env:ProgramFiles\nodejs\node.exe") { "$env:ProgramFiles\nodejs\node.exe" } else { $null }
    if (-not $exe) { return $null }
    try {
        $v = & $exe -p 'process.versions.node' 2>$null
        if ($LASTEXITCODE -ne 0) { return $null }
        $version = [version]($v.Trim())
        if ($version.Major -lt 24) { return $null }
        return @{ Exe = $exe; Version = $version }
    } catch { return $null }
}

# Windows OCR needs an installed language pack; scanned pages fail without one.
function Test-EnglishOcr {
    try {
        Add-Type -AssemblyName System.Runtime.WindowsRuntime -ErrorAction Stop
        $null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
        foreach ($language in [Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages) {
            if ($language.LanguageTag -like 'en-*') { return $true }
        }
        return $false
    } catch { return $null }
}

function Test-GfReady([int]$Port, [string]$TokenPath) {
    $token = Read-GfToken $TokenPath
    if (-not $token) { return $false }
    try {
        $status = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -Headers @{ Authorization = "Bearer $token" } -TimeoutSec 5 -UseBasicParsing
        return ($status.service -ceq 'Get Files' -and $status.ok -eq $true -and $status.worker_alive -eq $true)
    } catch { return $false }
}

# Top-level processes of this installation's service. A venv python.exe starts the real interpreter as a
# child, so a process belongs to us when it, or its parent, runs this installation's venv Python.
function Get-GfServiceProcesses([string]$Root) {
    $venv = (Join-Path $Root '.venv\Scripts\python.exe')
    $all = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue)
    $owned = @{}
    foreach ($p in $all) {
        if ([string]$p.CommandLine -notlike '*-m corpus*') { continue }
        if ([string]$p.ExecutablePath -ieq $venv) { $owned[[int]$p.ProcessId] = $true; continue }
        $parent = $all | Where-Object { $_.ProcessId -eq $p.ParentProcessId } | Select-Object -First 1
        if ($parent -and [string]$parent.ExecutablePath -ieq $venv) { $owned[[int]$parent.ProcessId] = $true }
    }
    return @($owned.Keys)
}

function Stop-GfService([string]$Root) {
    $stopped = $false
    foreach ($id in (Get-GfServiceProcesses $Root)) {
        $null = & taskkill.exe /PID $id /T /F 2>&1
        $stopped = $true
    }
    return $stopped
}

# Started through WMI: no window, and no console or output pipe shared with whatever ran Setup, so nothing
# waits on the service and closing a window does not stop it. The service keeps its own state and logs in data\.
function Start-GfService([string]$Root, [int]$Port) {
    $paths = Get-GfPaths $Root
    if (Test-GfReady $Port $paths.Token) { return $true }
    if (-not (Test-PortFree $Port)) { return $false }
    $environment = New-Object Collections.Generic.List[string]
    foreach ($name in @('SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'PATH', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'USERNAME', 'COMSPEC')) {
        $value = [Environment]::GetEnvironmentVariable($name)
        if ($value) { $environment.Add("$name=$value") }
    }
    $environment.Add("GET_FILES_DATA=$($paths.Data)")
    $startup = ([wmiclass]'Win32_ProcessStartup').CreateInstance()
    $startup.ShowWindow = 0
    $startup.CreateFlags = 8   # DETACHED_PROCESS: no console at all, so no terminal window can appear
    $startup.EnvironmentVariables = [string[]]$environment.ToArray()
    $result = ([wmiclass]'Win32_Process').Create("`"$($paths.Venv)`" -m corpus --data `"$($paths.Data)`" serve --host 127.0.0.1 --port $Port --token-file `"$($paths.Token)`"", $Root, $startup)
    if ($result.ReturnValue -ne 0) { return $false }
    $deadline = (Get-Date).AddSeconds(60)
    while ((Get-Date) -lt $deadline) {
        if (Test-GfReady $Port $paths.Token) { return $true }
        Start-Sleep -Seconds 1
    }
    return $false
}
