# Command-line client, runnable from any directory:
#   .\get-files.ps1 <url> [--pdf] [--mode download|corpus|both] [--collection NAME] [--tag TAG] [--output FOLDER]
#   .\get-files.ps1 --batch urls.txt --pdf
#   .\get-files.ps1 --jobs JOB_ID --wait 20
$ErrorActionPreference='Stop'
$node=(Get-Command node -ErrorAction Stop).Source
& $node (Join-Path $PSScriptRoot 'scripts\get-files.mjs') @args
exit $LASTEXITCODE
