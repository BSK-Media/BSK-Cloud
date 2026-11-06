<#
.SYNOPSIS
  Instaluje BSK Backup dla bieżącego użytkownika Windows (nie wymaga uprawnień administratora).

.EXAMPLE
  .\install-windows.ps1 -Server "https://cloud.bskmedia.pl" -Token "bsk_..."
  .\install-windows.ps1 -Server "https://cloud.bskmedia.pl" -Token "bsk_..." -Folders "C:\Projekty","D:\Klienci"

  Bez -Folders kopiowane są Pulpit i Dokumenty (także przekierowane do OneDrive).
  Jeśli PowerShell blokuje skrypty, uruchom:
  powershell -ExecutionPolicy Bypass -File .\install-windows.ps1 -Server ... -Token ...
#>
param(
  [Parameter(Mandatory = $true)][string]$Server,
  [Parameter(Mandatory = $true)][string]$Token,
  [string[]]$Folders = @()
)
$ErrorActionPreference = "Stop"
$src = $PSScriptRoot
$dest = Join-Path $env:LOCALAPPDATA "Programs\BSKBackup"

foreach ($exe in "bsk-backup.exe", "bsk-backup-agent.exe") {
  if (-not (Test-Path (Join-Path $src $exe))) { throw "Brak pliku $exe obok skryptu — najpierw zbuduj program (build-windows.bat)." }
}

# zatrzymaj starą wersję, jeśli działa
Get-Process -Name "bsk-backup-agent" -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 1

New-Item -ItemType Directory -Force -Path $dest | Out-Null
Copy-Item (Join-Path $src "bsk-backup.exe") $dest -Force
Copy-Item (Join-Path $src "bsk-backup-agent.exe") $dest -Force
Copy-Item (Join-Path $src "uninstall-windows.ps1") $dest -Force -ErrorAction SilentlyContinue

$cli = Join-Path $dest "bsk-backup.exe"
$cfgArgs = @("configure", "--server", $Server, "--token", $Token)
if ($Folders.Count -eq 0) {
  # Prawdziwe położenie Pulpitu i Dokumentów (uwzględnia przekierowanie do OneDrive)
  $Folders = @([Environment]::GetFolderPath("Desktop"), [Environment]::GetFolderPath("MyDocuments")) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -Unique
}
foreach ($f in $Folders) { $cfgArgs += @("--folder", $f) }
& $cli @cfgArgs
if ($LASTEXITCODE -ne 0) { throw "Konfiguracja nie powiodła się." }

& $cli test
if ($LASTEXITCODE -ne 0) {
  Write-Warning "Brak połączenia z serwerem teraz — agent i tak zacznie działać i wyśle pliki, gdy połączenie wróci."
}

# autostart przy logowaniu użytkownika
$agent = Join-Path $dest "bsk-backup-agent.exe"
Set-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -Name "BSKBackup" -Value "`"$agent`" run"

Start-Process -FilePath $agent -ArgumentList "run" -WindowStyle Hidden
Write-Host ""
Write-Host "BSK Backup zainstalowany w $dest i uruchomiony. Startuje automatycznie po zalogowaniu." -ForegroundColor Green
Write-Host "Podgląd stanu: & `"$cli`" status"
