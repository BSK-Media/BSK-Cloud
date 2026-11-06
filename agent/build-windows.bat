@echo off
REM Buduje BSK Backup do plików .exe (uruchom na dowolnym komputerze z Windows i Pythonem 3.11+).
REM Wynik: folder dist\ z bsk-backup.exe (konsola), bsk-backup-agent.exe (praca w tle) i skryptami instalacji.
setlocal
cd /d "%~dp0"
py -3 -m venv .build-venv || goto :error
call .build-venv\Scripts\activate.bat
python -m pip install --upgrade pip >nul
python -m pip install -r requirements.txt pyinstaller || goto :error
pyinstaller --noconfirm --onefile --name bsk-backup bsk_backup.py || goto :error
pyinstaller --noconfirm --onefile --noconsole --name bsk-backup-agent bsk_backup.py || goto :error
copy /y install-windows.ps1 dist\ >nul
copy /y uninstall-windows.ps1 dist\ >nul
echo.
echo Gotowe. Skopiuj zawartosc folderu dist\ na komputer pracownika.
goto :eof
:error
echo Blad budowania.
exit /b 1
