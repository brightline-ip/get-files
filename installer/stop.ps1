# Stops this installation's Get Files service and nothing else.
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
[void](Stop-GfService $root)
exit 0
