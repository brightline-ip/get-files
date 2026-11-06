@echo off
setlocal
rem Get Files - double-click to install or repair.
rem Windows PowerShell must not inherit PowerShell 7's module path.
set "PSModulePath="
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer\setup.ps1" %*
echo.
pause
