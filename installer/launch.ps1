# Starts Get Files if needed and says how to use it.
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$code = 0
try {
    $config = Read-GfConfig $root
    Write-Host 'Starting Get Files...'
    if (-not (Start-GfService $root ([int]$config.port))) {
        throw "Get Files did not start. If port $($config.port) is used by another program, stop it."
    }
    Write-Host "Get Files is running on this computer at http://127.0.0.1:$($config.port)." -ForegroundColor Green
    Write-Host 'Use it from an AI assistant (MCP), or from PowerShell:'
    Write-Host "  & '$root\get-files.ps1' https://example.org/paper.pdf --pdf"
    Read-Host 'Press Enter to close' | Out-Null
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    Read-Host 'Press Enter to close' | Out-Null
    $code = 1
}
exit $code
