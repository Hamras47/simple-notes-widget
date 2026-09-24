# Start the DeepSeek balance widget automatically with Windows.
# Adds a shortcut to the current user's Startup folder (no admin rights needed).
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
& "$here\.venv\Scripts\python.exe" "$here\app.py" --install-autostart
Write-Host ''
Write-Host "Startup folder: $env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup"
