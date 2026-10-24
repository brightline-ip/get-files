param([string]$ListenAddress='127.0.0.1',[int]$Port=8766,[switch]$Background,[string]$SpokeConfig='',[string]$DataPath='')
$ErrorActionPreference='Stop'
if ($ListenAddress -notin @('127.0.0.1','::1')) { throw 'Master-token API must use loopback; configure the separate TLS spoke for n8n.' }
. (Join-Path $PSScriptRoot 'scripts\runtime.ps1')
$pythonExe=Get-CorpusPython $PSScriptRoot
$launcherHash=[System.BitConverter]::ToString([System.Security.Cryptography.SHA256]::Create().ComputeHash([System.Text.Encoding]::UTF8.GetBytes($PSScriptRoot))).Replace('-','')
$launcherMutex=New-Object System.Threading.Mutex($false,"Local\GetFilesLauncher-$launcherHash")
$launcherOwned=$false
try {
try { $launcherOwned=$launcherMutex.WaitOne(30000) } catch [System.Threading.AbandonedMutexException] { $launcherOwned=$true }
if (-not $launcherOwned) { throw 'Another Get Files launcher is still starting.' }
if (-not $DataPath) { if ($env:GET_FILES_DATA) { $DataPath=$env:GET_FILES_DATA } else { $DataPath=Join-Path $PSScriptRoot 'data' } }
$dataPath=[System.IO.Path]::GetFullPath($DataPath)
New-Item -ItemType Directory -Force -Path $dataPath | Out-Null
$tokenPath=Join-Path $dataPath 'service-token.txt'
Initialize-CorpusToken $tokenPath
$serverArgs=@('-m','corpus','--data',$dataPath,'serve','--host',$ListenAddress,'--port',"$Port",'--token-file',$tokenPath)
if (-not $SpokeConfig) { $SpokeConfig=Join-Path $PSScriptRoot 'security\spoke.json' }
if (Test-Path -LiteralPath $SpokeConfig) {
    if ($SpokeConfig.Contains('"')) { throw 'Invalid configuration path' }
    $serverArgs+=@('--spoke-config',$SpokeConfig)
}
if ($Background) {
    try {
        $health=Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 2
        if ($health.ok -and $health.service -eq 'Get Files' -and $health.worker_alive) { Write-Host 'Get Files is already running.'; exit 0 }
        throw 'The selected port is occupied by another service.'
    } catch {
        if ($_.Exception.Message -eq 'The selected port is occupied by another service.') { throw }
    }
    $quotedArgs=@($serverArgs | ForEach-Object { if ($_.Contains('"')) { throw 'Invalid startup argument' }; '"'+$_+'"' })
    $process=Start-Process -FilePath $pythonExe -ArgumentList $quotedArgs -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $dataPath 'service.stdout.log') -RedirectStandardError (Join-Path $dataPath 'service.stderr.log')
    for ($attempt=0; $attempt -lt 40; $attempt++) {
        Start-Sleep -Milliseconds 250
        $process.Refresh()
        if ($process.HasExited) { throw 'Get Files process exited before becoming healthy; inspect local logs.' }
        try {
            $health=Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 1
            if ($health.ok -and $health.service -eq 'Get Files' -and $health.worker_alive) {
                # Process details are only returned to authenticated callers.
                $detail=Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 1 -Headers @{Authorization='Bearer '+(Get-Content -LiteralPath $tokenPath -Raw).Trim()}
                @{pid=$detail.process_id;launcher_pid=$process.Id;port=$Port;listen_address=$ListenAddress} | ConvertTo-Json | Set-Content (Join-Path $dataPath 'service-state.json')
                Write-Host "Get Files running on port $Port; authentication configured."
                exit 0
            }
        } catch {}
    }
    throw 'Get Files did not become healthy. Check data/service.stderr.log.'
}
$launcherMutex.ReleaseMutex()
$launcherOwned=$false
Push-Location -LiteralPath $PSScriptRoot
try { & $pythonExe @serverArgs }
finally { Pop-Location }
} finally { if ($launcherOwned) { $launcherMutex.ReleaseMutex() }; $launcherMutex.Dispose() }
