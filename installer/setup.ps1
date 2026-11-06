<#
.SYNOPSIS
  Installs Get Files for the current Windows user. Started by Setup.cmd.

.DESCRIPTION
  Copies Get Files into a per-user folder, creates its Python environment and service token, adds Start Menu
  shortcuts and starts the local service. Missing programs are explained and offered through winget;
  nothing is installed without your "yes". Re-running Setup repairs or updates an installation and keeps your
  data and token. No administrator rights are needed for Get Files itself.

.PARAMETER InstallRoot
  Install folder. Default: %LOCALAPPDATA%\Brightline\GetFiles
.PARAMETER Port
  Local port. Default 8766. With another port, MCP clients need GET_FILES_URL (Setup writes it for you).
.PARAMETER ShortcutFolder
  Where shortcuts go. Default: Start Menu, folder "Get Files".
.PARAMETER DryRun
  Check this computer and show what Setup would do, without installing, writing or starting anything.
.PARAMETER SimulateMissing
  With -DryRun only: pretend these programs are missing (Python, Node).
.PARAMETER NoOptional
  Do not offer Node.js. Never approves installing software.
#>
[CmdletBinding()]
param(
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'Brightline\GetFiles'),
    [ValidateRange(1024, 65535)][int]$Port = 8766,
    [string]$ShortcutFolder = (Join-Path ([Environment]::GetFolderPath('Programs')) 'Get Files'),
    [string]$PythonExe,
    [switch]$DryRun,
    [string[]]$SimulateMissing = @(),
    [switch]$NoOptional,
    [switch]$NoStart
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
$bundle = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$InstallRoot = [IO.Path]::GetFullPath($InstallRoot).TrimEnd('\')
$paths = Get-GfPaths $InstallRoot
$PowerShellExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$SimulateMissing = @($SimulateMissing | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
if ($SimulateMissing.Count -and -not $DryRun) { throw '-SimulateMissing can only be used together with -DryRun.' }

function Get-WingetArgs([string]$Id) {
    return @('install', '--id', $Id, '--exact', '--source', 'winget', '--accept-source-agreements', '--accept-package-agreements')
}

# Explain a missing program and, after an explicit yes, install it with winget.
# Returns 'installed', 'declined', 'failed' or 'dryrun'.
function Request-Program([string]$Name, [string]$Id, [string[]]$Why, [string]$ManualUrl) {
    Write-Warn2 "$Name was not found."
    foreach ($line in $Why) { Write-Info $line }
    Write-Info 'Setup can install it with Windows Package Manager:'
    Write-Info ('    winget ' + ((Get-WingetArgs $Id) -join ' '))
    Write-Info "Or install it yourself from $ManualUrl and run Setup again."
    if ($DryRun) { Write-Info "[dry run] Setup would ask: Install $Name now?"; return 'dryrun' }
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $winget) {
        Write-Bad 'Windows Package Manager (winget) is not available. Install "App Installer" from the Microsoft Store, or use the link above.'
        return 'failed'
    }
    if (-not (Read-YesNo "Install $Name now?" $false)) { return 'declined' }
    Write-Info "Installing $Name. Windows may ask for permission; choose Yes."
    & $winget.Source @(Get-WingetArgs $Id)
    $code = $LASTEXITCODE
    Update-SessionPath
    # -1978335189 (0x8A15002B): already installed, no applicable upgrade.
    if ($code -eq 0 -or $code -eq -1978335189) { return 'installed' }
    Write-Bad "winget reported an error ($code) while installing $Name."
    return 'failed'
}

function New-GfShortcut([string]$Name, [string]$Script, [string]$Description, [string]$Icon, [switch]$Hidden) {
    New-Item -ItemType Directory -Force -Path $ShortcutFolder | Out-Null
    $lnk = Join-Path $ShortcutFolder ($Name + '.lnk')
    $s = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
    $s.TargetPath = $PowerShellExe
    $window = if ($Hidden) { '-WindowStyle Hidden ' } else { '' }
    $s.Arguments = '-NoProfile -ExecutionPolicy Bypass -STA ' + $window + '-File "' + (Join-Path $InstallRoot "installer\$Script") + '"'
    $s.WorkingDirectory = $InstallRoot
    $s.Description = $Description
    $s.IconLocation = "$env:SystemRoot\System32\$Icon"
    $s.Save()
    return $lnk
}

$exit = 0
try {
    Write-Host ''
    Write-Host 'Get Files - Setup (public preview 0.1.0)' -ForegroundColor White
    Write-Info 'Downloads source documents into a hash-verified, searchable library on your own computer.'
    Write-Info "Install folder: $InstallRoot"

    # ---- 1. This computer --------------------------------------------------------------------
    Write-Step 'Step 1 of 5: Checking this computer'
    if (-not [Environment]::Is64BitOperatingSystem -or [Environment]::OSVersion.Version.Major -lt 10) { throw 'Get Files needs 64-bit Windows 10 or 11.' }
    if (-not (Test-Path -LiteralPath (Join-Path $bundle 'corpus\server.py'))) {
        throw 'The download is incomplete. Extract the whole zip file (right-click, Extract All) and run Setup.cmd from the extracted folder.'
    }
    if ($InstallRoot.IndexOfAny([char[]]'"%!') -ge 0) { throw 'The install folder path cannot contain ", % or !. Choose another with -InstallRoot.' }
    $drive = [IO.Path]::GetPathRoot($InstallRoot)
    $info = New-Object IO.DriveInfo($drive)
    # The service token and your library rely on NTFS permissions.
    if ($info.DriveFormat -notin @('NTFS', 'ReFS')) { throw "The install folder is on a $($info.DriveFormat) drive, which cannot restrict access to your library and token. Choose an NTFS drive with -InstallRoot." }
    if ($InstallRoot.Length -gt 80) { throw 'The install folder path is too long (OCR working folders need the room). Choose a shorter path with -InstallRoot.' }
    if ($info.AvailableFreeSpace -lt 2GB) { Write-Warn2 ("Only {0:N1} GB free on {1}. Allow at least 2 GB plus room for your documents." -f ($info.AvailableFreeSpace / 1GB), $drive) }
    Write-Good ('Windows ' + [Environment]::OSVersion.Version.ToString() + ', 64-bit')

    # ---- 2. Programs --------------------------------------------------------------------------
    Write-Step 'Step 2 of 5: Checking required programs'
    $python = if ('Python' -in $SimulateMissing) { $null } else { Find-GfPython $PythonExe }
    if (-not $python) {
        $r = Request-Program 'Python 3.12 or later (required)' 'Python.Python.3.12' @('Get Files is written in Python.') 'https://www.python.org/downloads/windows/'
        if ($r -eq 'installed') { $python = Find-GfPython $PythonExe }
        if (-not $python -and $r -ne 'dryrun') { throw 'Python 3.12 or later is required. Install it and run Setup again. If you just installed it, close this window and run Setup again.' }
    }
    if ($python) {
        Write-Good "Python $($python.Version) ($($python.Exe))"
        if ($python.Version.Minor -ne 12) { Write-Info 'Note: the tests run on Python 3.12; newer versions are expected to work.' }
    }

    $node = if ('Node' -in $SimulateMissing) { $null } else { Find-GfNode }
    if (-not $node -and -not $NoOptional) {
        $r = Request-Program 'Node.js 24 or later (recommended)' 'OpenJS.NodeJS.LTS' @('Needed to use Get Files from AI assistants (MCP) and from the get-files command.', 'Without it only the local REST API works.') 'https://nodejs.org/'
        if ($r -eq 'installed') { $node = Find-GfNode }
    }
    if ($node) { Write-Good "Node.js $($node.Version)" } else { Write-Warn2 'Continuing without Node.js: only the local REST API works. Run Setup again after installing it.' }

    $ocr = Test-EnglishOcr
    if ($ocr -eq $true) { Write-Good 'Windows English OCR language pack' }
    elseif ($ocr -eq $false) {
        Write-Warn2 'The Windows English OCR language pack was not found, so scanned PDF pages stay unread. Other files work.'
        Write-Info 'Add it in Settings, Time and language, Language and region, English, Language options, Optical character recognition.'
    } else { Write-Info 'Could not check for the Windows OCR language pack.' }

    # ---- 3. Port ------------------------------------------------------------------------------
    Write-Step 'Step 3 of 5: Checking the local port'
    $hadInstall = Test-Path -LiteralPath $paths.Marker -PathType Leaf
    if (-not $hadInstall -and (Test-Path -LiteralPath $InstallRoot) -and (Get-ChildItem -LiteralPath $InstallRoot -Force | Select-Object -First 1)) {
        throw "$InstallRoot already has files that Setup did not put there. Choose an empty folder with -InstallRoot."
    }
    $ownRunning = $hadInstall -and @(Get-GfServiceProcesses $InstallRoot).Count -gt 0
    if ($hadInstall -and -not $DryRun) {
        if (Stop-GfService $InstallRoot) { Write-Info 'Stopped the earlier installation so it can be updated (your data is kept).'; Start-Sleep -Seconds 2 }
    }
    if (-not ($DryRun -and $ownRunning) -and -not (Test-PortFree $Port)) {
        throw "Port $Port is already used by another program. Stop that program, or run Setup again with another port, for example: Setup.cmd -Port 18766"
    }
    Write-Good "Port $Port is free (this computer only)"

    if ($DryRun) {
        Write-Host ''
        Write-Good 'Dry run finished. Nothing was changed.'
        Write-Info "Setup would copy the program to $InstallRoot, run install.ps1, add shortcuts in $ShortcutFolder and start the service."
        exit 0
    }

    # ---- 4. Install ---------------------------------------------------------------------------
    Write-Step 'Step 4 of 5: Installing (downloads Python and Node packages; allow a few minutes)'
    New-Item -ItemType Directory -Force -Path $InstallRoot | Out-Null
    New-Item -ItemType Directory -Force -Path $paths.Logs | Out-Null
    if (-not $hadInstall) { Write-Utf8NoBom $paths.Marker "Get Files install root. The uninstaller removes only folders that contain this file.`r`n" }
    if ($bundle -ine $InstallRoot) {
        $null = & robocopy.exe $bundle $InstallRoot /MIR /XD .git .venv node_modules data intake security logs __pycache__ /XF installer.json MCP-setup.json $script:GfMarker /NFL /NDL /NJH /NJS /NP
        if ($LASTEXITCODE -ge 8) { throw "Copying the program files failed (robocopy code $LASTEXITCODE)." }
    }
    # Clear the "downloaded from the Internet" mark on the copies this installation runs.
    Get-ChildItem -LiteralPath $InstallRoot -Recurse -File -Include *.ps1, *.cmd, *.py, *.mjs, *.js | Unblock-File -ErrorAction SilentlyContinue
    Write-Good 'Program files copied'

    $log = Join-Path $paths.Logs 'install.log'
    $installArgs = @('-PythonExe', $python.Exe, '-DataPath', $paths.Data)
    if (-not $node) { $installArgs += '-SkipNode' }
    $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    & $PowerShellExe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $InstallRoot 'install.ps1') @installArgs *> $log
    $code = $LASTEXITCODE
    $ErrorActionPreference = $old
    if ($code -ne 0) { throw "Installation failed. Details: $log" }
    if (-not (Test-Path -LiteralPath $paths.Token -PathType Leaf)) { throw "The service token was not created. Details: $log" }
    Write-Good 'Python environment and service token ready (the library and token are readable only by your account)'

    # ---- 5. Shortcuts and first start ---------------------------------------------------------
    Write-Step 'Step 5 of 5: Shortcuts and first start'
    $shortcuts = @(
        (New-GfShortcut 'Start Get Files' 'launch.ps1' 'Start Get Files' 'shell32.dll,13'),
        (New-GfShortcut 'Stop Get Files' 'stop.ps1' 'Stop the Get Files service' 'shell32.dll,131' -Hidden),
        (New-GfShortcut 'Uninstall Get Files' 'remove.ps1' 'Remove Get Files' 'shell32.dll,31')
    )
    Write-Good "Shortcuts created in $ShortcutFolder"

    $config = [ordered]@{ version = '0.1.0'; port = $Port; shortcuts = @($shortcuts); shortcut_folder = $ShortcutFolder }
    Write-Utf8NoBom $paths.Config ($config | ConvertTo-Json -Depth 4)

    $entry = [ordered]@{ command = $(if ($node) { $node.Exe } else { 'node' }); args = @((Join-Path $InstallRoot 'integrations\mcp\server.mjs')); env = [ordered]@{ GET_FILES_DATA = $paths.Data } }
    if ($Port -ne 8766) { $entry['env']['GET_FILES_URL'] = "http://127.0.0.1:$Port" }
    $mcp = [ordered]@{ mcpServers = [ordered]@{ get_files = $entry } } | ConvertTo-Json -Depth 5
    Write-Utf8NoBom (Join-Path $InstallRoot 'MCP-setup.json') $mcp

    if (-not $NoStart) {
        if (-not (Start-GfService $InstallRoot $Port)) { throw "Get Files did not start. To see why, run this in PowerShell: & '$InstallRoot\start.ps1'" }
        Write-Good "Get Files is running at http://127.0.0.1:$Port (this computer only)"
    }

    Write-Host ''
    Write-Good 'Setup finished.'
    Write-Info "Try it: & '$InstallRoot\get-files.ps1' https://example.org/paper.pdf --pdf"
    if ($node) { Write-Info "To connect an AI assistant, merge $(Join-Path $InstallRoot 'MCP-setup.json') into its MCP configuration." }
    Write-Info 'Your library stays in the install folder; exports go to Documents\Get Files. Local files are read only from the intake folder.'
    Write-Info 'After restarting Windows, "Start Get Files" starts it again. AI assistants start it on demand.'
    Write-Info 'Read DISCLAIMER.md: you are responsible for the terms of each source; extraction can be wrong; this is not legal advice.'
} catch {
    Write-Host ''
    Write-Bad $_.Exception.Message
    Write-Info 'Nothing outside the install folder and shortcuts was changed. You can fix the problem and run Setup again.'
    $exit = 1
}
exit $exit
