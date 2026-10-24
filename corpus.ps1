$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'scripts\runtime.ps1')
$pythonExe = Get-CorpusPython $PSScriptRoot
Push-Location -LiteralPath $PSScriptRoot
try { & $pythonExe -m corpus @args; if ($LASTEXITCODE -ne 0) { throw 'Get Files command failed.' } }
finally { Pop-Location }
