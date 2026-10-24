function Initialize-CorpusToken([string]$TokenPath) {
    if (Test-Path -LiteralPath $TokenPath) { return }
    $bytes=New-Object byte[] 48
    $random=[System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $random.GetBytes($bytes) } finally { $random.Dispose() }
    # The token file is created owner-only (inheritance removed) so no secret is ever written under inherited grants.
    $acl=New-Object System.Security.AccessControl.FileSecurity
    $acl.SetAccessRuleProtection($true,$false)
    $owner=[System.Security.Principal.WindowsIdentity]::GetCurrent().User
    $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule($owner,'FullControl','Allow')))
    $stream=$null
    try {
        # CreateNew prevents concurrent launchers from replacing an existing token.
        $rights=[System.Security.AccessControl.FileSystemRights]::Write
        if ($PSVersionTable.PSEdition -eq 'Core') {
            $stream=[System.IO.FileSystemAclExtensions]::Create((New-Object System.IO.FileInfo($TokenPath)),[System.IO.FileMode]::CreateNew,$rights,[System.IO.FileShare]::None,4096,[System.IO.FileOptions]::None,$acl)
        } else {
            $stream=New-Object System.IO.FileStream($TokenPath,[System.IO.FileMode]::CreateNew,$rights,[System.IO.FileShare]::None,4096,[System.IO.FileOptions]::None,$acl)
        }
    } catch [System.IO.IOException] {
        if (Test-Path -LiteralPath $TokenPath) { return }
        throw
    }
    try {
        $encodedBytes=[System.Text.Encoding]::ASCII.GetBytes([Convert]::ToBase64String($bytes))
        $stream.Write($encodedBytes,0,$encodedBytes.Length)
        $stream.Flush()
    } finally { $stream.Dispose() }
}

function Get-CorpusPython([string]$ProjectRoot) {
    if ($env:GET_FILES_PYTHON) {
        if (-not (Test-Path -LiteralPath $env:GET_FILES_PYTHON)) { throw 'GET_FILES_PYTHON does not point to an existing interpreter.' }
        return $env:GET_FILES_PYTHON
    }
    $candidate=Join-Path $ProjectRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $candidate)) {
        throw 'A project virtual environment is required. Run install.ps1 first (see README.md).'
    }
    return $candidate
}
