<#
.SYNOPSIS
  Removes what install.ps1 created: the running service (if any), .venv and MCP node_modules.

.DESCRIPTION
  Your library (the data directory: originals, extracted text, database, service token) is KEPT unless you pass
  -RemoveData. Exported copies under the export root are never touched. Remove the MCP registration from your
  client configuration yourself. Use -WhatIf to preview.

.PARAMETER DataPath
  Data directory (default: $env:GET_FILES_DATA, else <install dir>\data).
.PARAMETER RemoveData
  Also delete the data directory. This permanently deletes the downloaded library and cannot be undone.
#>
[CmdletBinding(SupportsShouldProcess,ConfirmImpact='High')]
param([string]$DataPath='',[switch]$RemoveData)
$ErrorActionPreference='Stop'
$root=$PSScriptRoot
if (-not $DataPath) { if ($env:GET_FILES_DATA) { $DataPath=$env:GET_FILES_DATA } else { $DataPath=Join-Path $root 'data' } }
$DataPath=[System.IO.Path]::GetFullPath($DataPath)

# Stop the service only if the recorded process is a corpus server using this data directory.
$stateFile=Join-Path $DataPath 'service-state.json'
if (Test-Path -LiteralPath $stateFile) {
    try {
        $state=Get-Content -Raw -LiteralPath $stateFile | ConvertFrom-Json
        $p=Get-CimInstance Win32_Process -Filter "ProcessId=$([int]$state.pid)" -ErrorAction SilentlyContinue
        if ($p -and $p.CommandLine -match '-m"?\s+"?corpus' -and $p.CommandLine.Contains($DataPath)) {
            if ($PSCmdlet.ShouldProcess("process $($p.ProcessId)",'Stop Get Files service')) { Stop-Process -Id $p.ProcessId }
        }
    } catch { Write-Warning 'Could not inspect the recorded service process; stop it manually if it is running.' }
}

foreach ($target in @((Join-Path $root '.venv'),(Join-Path $root 'integrations\mcp\node_modules'))) {
    if (Test-Path -LiteralPath $target) {
        if ($PSCmdlet.ShouldProcess($target,'Remove')) { Remove-Item -LiteralPath $target -Recurse -Force }
    }
}

if ($RemoveData) {
    if (Test-Path -LiteralPath $DataPath) {
        if ($PSCmdlet.ShouldProcess($DataPath,'PERMANENTLY DELETE library and data')) { Remove-Item -LiteralPath $DataPath -Recurse -Force }
    }
} else {
    Write-Host "Kept your library at: $DataPath (re-run with -RemoveData to delete it)."
}
Write-Host 'Remove the "get_files" entry from your MCP client configuration if you added one.'
