<#
.SYNOPSIS
  Sets up Get Files in place: Python virtual environment, Node MCP dependencies, local service token.

.DESCRIPTION
  - Creates .venv in the install directory and installs requirements.lock (needs network access to PyPI).
  - Runs `npm ci --ignore-scripts` for integrations/mcp (needs network access to the npm registry).
  - Creates the data directory and a random service token readable only by the current user. The directory's own
    access list is restricted only when the script creates it or it is empty; existing non-empty folders are not changed.
  - Prints the MCP client registration snippet.
  Nothing is started and no scheduled task or service is installed. Re-running is safe: existing environments
  and an existing token are kept.

.PARAMETER DataPath
  Data directory (default: $env:GET_FILES_DATA, else <install dir>\data).
.PARAMETER PythonExe
  Python 3.12 interpreter to build the virtual environment from (default: py -3.12, else python on PATH).
.PARAMETER SkipPython
  Do not create the virtual environment.
.PARAMETER SkipNode
  Do not install the MCP server's Node dependencies.
#>
param([string]$DataPath='',[string]$PythonExe='',[switch]$SkipPython,[switch]$SkipNode)
$ErrorActionPreference='Stop'
if ($env:OS -ne 'Windows_NT') { throw 'Get Files supports Windows only (Windows OCR and DPAPI are required).' }
$root=$PSScriptRoot
. (Join-Path $root 'scripts\runtime.ps1')

if (-not $DataPath) { if ($env:GET_FILES_DATA) { $DataPath=$env:GET_FILES_DATA } else { $DataPath=Join-Path $root 'data' } }
$DataPath=[System.IO.Path]::GetFullPath($DataPath)

if (-not $SkipPython) {
    $venv=Join-Path $root '.venv'
    $venvPython=Join-Path $venv 'Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $venvPython)) {
        Write-Host 'Creating virtual environment (.venv)...'
        if ($PythonExe) { & $PythonExe -m venv $venv }
        elseif (Get-Command py -ErrorAction SilentlyContinue) { & py -3.12 -m venv $venv }
        else { & python -m venv $venv }
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $venvPython)) { throw 'Could not create the virtual environment. Python 3.12 is required.' }
    }
    $version=& $venvPython -c 'import sys;print("%d.%d"%sys.version_info[:2])'
    if ($version -ne '3.12') { Write-Warning "Python $version found; Python 3.12 is the tested version." }
    Write-Host 'Installing Python packages from requirements.lock...'
    & $venvPython -m pip install --disable-pip-version-check -r (Join-Path $root 'requirements.lock')
    if ($LASTEXITCODE -ne 0) { throw 'pip install failed.' }
}

if (-not $SkipNode) {
    $npm=Get-Command npm -ErrorAction SilentlyContinue
    if (-not $npm) { throw 'Node.js (with npm) is required for the MCP server and CLI. Install Node 24 or newer, or use -SkipNode.' }
    Write-Host 'Installing MCP server dependencies (npm ci)...'
    & npm ci --prefix (Join-Path $root 'integrations\mcp') --ignore-scripts
    if ($LASTEXITCODE -ne 0) { throw 'npm ci failed.' }
}

# Data directory and service token: current user only.
# Only a directory this script creates, or one that is still empty, has its access list rewritten. An existing,
# non-empty folder (for example Documents) is left untouched: changing its ACL would not be undone by uninstall.ps1.
$dataIsNew=-not (Test-Path -LiteralPath $DataPath)
$dataIsEmpty=$dataIsNew -or -not (Get-ChildItem -LiteralPath $DataPath -Force | Select-Object -First 1)
New-Item -ItemType Directory -Force -Path $DataPath | Out-Null
$user=[System.Security.Principal.WindowsIdentity]::GetCurrent().Name
if ($dataIsEmpty) {
    & icacls.exe $DataPath /inheritance:r /grant:r "${user}:(OI)(CI)F" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not restrict the data directory to the current user.' }
} else {
    Write-Warning "The data directory already contains files, so its access list was left unchanged: $DataPath. Only the service token file is restricted to the current user. Use a dedicated, empty folder for GET_FILES_DATA or -DataPath if you want the whole directory restricted."
}
$tokenPath=Join-Path $DataPath 'service-token.txt'
Initialize-CorpusToken $tokenPath
& icacls.exe $tokenPath /inheritance:r /grant:r "${user}:F" | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Could not restrict the service token to the current user.' }

$node=(Get-Command node -ErrorAction SilentlyContinue)
$nodePath=if ($node) { $node.Source } else { 'C:\\Program Files\\nodejs\\node.exe' }
$server=(Join-Path $root 'integrations\mcp\server.mjs')
$snippet=[ordered]@{ mcpServers=[ordered]@{ get_files=[ordered]@{ command=$nodePath; args=@($server);
    env=[ordered]@{ GET_FILES_DATA=$DataPath } } } } | ConvertTo-Json -Depth 6

Write-Host ''
Write-Host "Installed. Data directory: $DataPath"
Write-Host 'Start the service (optional; the MCP server and CLI start it on demand):'
Write-Host "  & '$root\start.ps1' -Background"
Write-Host ''
Write-Host 'Register with an MCP client (JSON "mcpServers" configuration):'
Write-Host $snippet
Write-Host ''
Write-Host 'For Codex-style TOML clients:'
Write-Host '  [mcp_servers.get_files]'
Write-Host "  command = '$nodePath'"
Write-Host "  args = ['$server']"
Write-Host "  env = { GET_FILES_DATA = '$DataPath' }"
