# Usuwa BSK Backup z komputera (kopie na NAS pozostają nienaruszone).
$ErrorActionPreference = "SilentlyContinue"
Get-Process -Name "bsk-backup-agent" | Stop-Process -Force
Remove-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -Name "BSKBackup"
Start-Sleep -Seconds 1
Remove-Item -Recurse -Force (Join-Path $env:LOCALAPPDATA "Programs\BSKBackup")
$keep = Read-Host "Usunąć też konfigurację i dziennik (%LOCALAPPDATA%\BSKBackup)? [t/N]"
if ($keep -match '^[tTyY]') { Remove-Item -Recurse -Force (Join-Path $env:LOCALAPPDATA "BSKBackup") }
Write-Host "BSK Backup odinstalowany."
