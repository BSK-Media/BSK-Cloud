# BSK Media Cloud — portal plików dla klientów + kopie komputerów pracowników

Dwa programy, które razem działają na Twoim **Ugreen NAS DXP4800 Plus**:

| Część | Gdzie działa | Co robi |
|---|---|---|
| **Portal `cloud.bskmedia.pl`** (`server/`) | Docker na NAS | Klienci logują się na konta założone przez Ciebie, wysyłają pliki robocze, piszą wiadomości/komentarze i pobierają pliki, które im udostępnisz. Ty jako admin zarządzasz kontami i widzisz wszystko w jednym panelu. |
| **BSK Backup** (`agent/`) | komputery pracowników (Windows; macOS/Linux też) | Działa w tle, wykrywa każdy nowo utworzony lub zapisany plik w Pulpicie/Dokumentach (lub wskazanych folderach) i wysyła go na NAS. Bez internetu pliki czekają w kolejce i wychodzą, gdy połączenie wróci. Starsze wersje plików są zachowywane. |

Pliki lądują na NAS w zwykłych folderach — widać je też w menedżerze plików UGOS:

```
/volume1/bsk-cloud/
├── klienci/<login-klienta>/…              pliki od klienta
│                        └── od BSK Media/ pliki, które udostępniłeś klientowi
├── kopie-pracownikow/<komputer>/Desktop/…  aktualne kopie
│                              └── .wersje/ poprzednie wersje (domyślnie 10 na plik)
└── app/                                   baza danych portalu (nie ruszać)
```

---

## 1. Instalacja portalu na NAS

### 1.1 Przygotowanie NAS
1. **Panel sterowania → Pliki → Folder współdzielony**: utwórz folder `bsk-cloud` (na woluminie 1).
2. **App Center**: zainstaluj aplikację **Docker**.
3. **Panel sterowania → Terminal**: włącz **SSH** (po instalacji możesz wyłączyć).
4. Skopiuj katalog `bsk-cloud/` z tego repozytorium na NAS, np. do `/volume1/docker/bsk-cloud`
   (przez udział SMB z komputera albo `scp -r bsk-cloud admin@IP-NAS:/volume1/docker/`).

### 1.2 Konfiguracja
Zaloguj się przez SSH (`ssh twoj-login@IP-NAS`) i:

```bash
cd /volume1/docker/bsk-cloud
cp .env.example .env
openssl rand -hex 32          # wynik wklej do BSK_SECRET_KEY w .env
ls -ln /volume1               # UID i GID właściciela folderu bsk-cloud -> PUID/PGID w .env
nano .env                     # uzupełnij BSK_SECRET_KEY, BSK_ADMIN_PASSWORD, PUID, PGID
```

Hasło admina: min. 10 znaków, małe i wielkie litery oraz cyfra.

### 1.3 Uruchomienie
```bash
sudo docker compose up -d --build
sudo docker compose logs -f app      # Ctrl+C aby wyjść
```
Test w sieci firmowej: `http://IP-NAS:8090` (logowanie po HTTP zadziała dopiero po HTTPS albo
tymczasowo z `BSK_COOKIE_SECURE=false`).

> Możesz też użyć interfejsu **Docker → Projekt → Utwórz** w UGOS: wskaż folder
> `/volume1/docker/bsk-cloud`, a plik `docker-compose.yml` zostanie wczytany automatycznie.

### 1.4 Adres cloud.bskmedia.pl (HTTPS)

**Wariant A — przekierowanie portów (masz publiczne IP):**
1. U rejestratora domeny `bskmedia.pl` dodaj rekord **A**: `cloud` → Twój publiczny adres IP
   (najlepiej stały; przy zmiennym użyj DDNS, np. wbudowanego w router).
2. Na routerze przekieruj porty: **80 → IP-NAS:8880** oraz **443 → IP-NAS:8443**.
3. Caddy sam pobierze certyfikat Let's Encrypt. Wejdź na `https://cloud.bskmedia.pl`.

**Wariant B — Cloudflare Tunnel (bez otwierania portów, działa też przy CGNAT):**
1. Domena `bskmedia.pl` musi mieć DNS w Cloudflare (darmowy plan wystarczy).
2. Cloudflare Zero Trust → Networks → Tunnels → Create tunnel → skopiuj token do
   `CLOUDFLARE_TUNNEL_TOKEN` w `.env`.
3. W tunelu dodaj *Public hostname*: `cloud.bskmedia.pl` → `http://app:8000`.
4. Uruchom: `sudo docker compose --profile tunnel up -d --build`
   (kontener `caddy` możesz wtedy usunąć z pliku compose). Uwaga: darmowy Cloudflare
   ogranicza pojedyncze żądanie do 100 MB — przy większych plikach wybierz wariant A.

### 1.5 Pierwsze logowanie
Zaloguj się kontem z `.env`, a potem **usuń `BSK_ADMIN_PASSWORD` z pliku `.env`**
(konto już istnieje w bazie). Hasło zmienisz w zakładce *Konto*.

---

## 2. Praca z klientami
1. **+ Klient** → login, nazwa, firma, e-mail. Hasło startowe jest proponowane automatycznie.
2. Po zapisaniu zobaczysz **jednorazowo** adres, login i hasło do przekazania klientowi.
   Klient przy pierwszym logowaniu musi ustawić własne hasło.
3. Na stronie klienta: jego pliki, wątek wiadomości, wysyłanie mu plików („od BSK Media”),
   reset hasła i blokada konta.
4. Opcjonalnie: powiadomienia e-mail o nowych plikach/wiadomościach — uzupełnij `BSK_SMTP_*`
   i `BSK_NOTIFY_EMAIL` w `.env`.

Klient widzi wyłącznie własne pliki i wiadomości. Może usuwać tylko to, co sam wysłał.

---

## 3. Kopie zapasowe komputerów pracowników

### 3.1 Dodaj komputer w portalu
**+ Komputer** → nazwa (np. `LAPTOP-ANNA`) i pracownik → zapisz. Pojawi się **token**
i gotowe polecenie instalacji (token jest widoczny tylko raz; w razie potrzeby wygenerujesz nowy).

### 3.2 Zbuduj program (raz)
Na dowolnym komputerze z Windows i [Pythonem 3.11+](https://www.python.org/downloads/):
```bat
cd bsk-cloud\agent
build-windows.bat
```
W folderze `agent\dist\` powstaną `bsk-backup.exe`, `bsk-backup-agent.exe` oraz skrypty
instalacji. Ten folder kopiujesz na komputery pracowników (pendrive, udział sieciowy).

### 3.3 Instalacja u pracownika
W folderze z programem (PowerShell, **bez** uprawnień administratora — instaluje się dla
zalogowanego użytkownika):
```powershell
powershell -ExecutionPolicy Bypass -File .\install-windows.ps1 -Server "https://cloud.bskmedia.pl" -Token "bsk_..."
```
Domyślnie kopiowane są **Pulpit** i **Dokumenty** (również przekierowane do OneDrive).
Inne foldery: dodaj `-Folders "C:\Projekty","D:\Klienci"`.

Agent startuje automatycznie po zalogowaniu do Windows. Podgląd stanu:
```powershell
& "$env:LOCALAPPDATA\Programs\BSKBackup\bsk-backup.exe" status
```
Dziennik: `%LOCALAPPDATA%\BSKBackup\bsk-backup.log`. Deinstalacja: `uninstall-windows.ps1`.

### 3.4 Jak działa agent
- reaguje na utworzenie, zapis, przeniesienie i skopiowanie pliku; czeka ~10 s, aż plik
  przestanie się zmieniać, liczy SHA-256 i wysyła tylko faktycznie zmienione pliki;
- pomija pliki tymczasowe i systemowe (`~$*.docx`, `*.tmp`, `Thumbs.db`, `desktop.ini`,
  `node_modules`, `.git` …) — lista `excludes` w `%LOCALAPPDATA%\BSKBackup\config.json`;
- przy starcie i co godzinę skanuje foldery, więc nic nie umknie, nawet gdy komputer był wyłączony;
- bez internetu/NAS pliki czekają w trwałej kolejce i są wysyłane automatycznie po powrocie
  połączenia (także po restarcie komputera);
- **usunięcie pliku na komputerze nie usuwa go z kopii**; nadpisanie tworzy nową wersję
  na NAS (stare w `.wersje/`, liczba w `BSK_BACKUP_KEEP_VERSIONS`);
- w portalu (*Komputery → nazwa*) wyszukasz plik i pobierzesz dowolną wersję;
- wyłączenie urządzenia lub nowy token natychmiast odcina stary komputer (np. po zwolnieniu
  pracownika lub kradzieży laptopa).

**macOS / Linux:** `python3 -m venv venv && venv/bin/pip install -r requirements.txt`, potem
`venv/bin/python bsk_backup.py configure --server … --token …` i autostart z
`macos-launchagent.plist` (macOS) lub usługi systemd (Linux).

---

## 4. Bezpieczeństwo i utrzymanie
- Hasła: bcrypt; blokada po wielu nieudanych logowaniach; ochrona CSRF; ciasteczka tylko
  po HTTPS; nagłówki bezpieczeństwa (CSP, HSTS); tokeny urządzeń trzymane jako skrót SHA-256.
- Każda operacja (logowanie, wysłanie, usunięcie, zmiany kont) trafia do *Dziennika*.
- **RAID to nie kopia zapasowa.** Skonfiguruj w UGOS (*Kopia zapasowa i synchronizacja*)
  kopię folderu `bsk-cloud` na dysk USB i/lub do chmury — zasada 3-2-1.
- Aktualizacja portalu: wgraj nowe pliki i `sudo docker compose up -d --build`.
  Dane w `/volume1/bsk-cloud` pozostają.

## 5. Dla programisty
```bash
cd server && pip install -r requirements-dev.txt && python -m pytest -q
cd agent  && pip install -r requirements.txt pytest uvicorn && python -m pytest -q   # test end-to-end z serwerem
```
Lokalny start serwera:
```bash
cd server
BSK_DATA_DIR=./dev-data BSK_SECRET_KEY=$(openssl rand -hex 32) BSK_COOKIE_SECURE=false \
BSK_ADMIN_USERNAME=admin BSK_ADMIN_PASSWORD=ZmienMnie123 \
uvicorn app.main:create_app --factory --reload
```
