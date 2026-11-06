# BSK Media Cloud

Własna „chmura” firmy BSK Media działająca na **Ugreen NAS DXP4800 Plus** pod adresem
**https://cloud.bskmedia.pl**:

- **Portal dla klientów.** Klienci logują się na konta, które dla nich zakładasz. Wysyłają pliki
  robocze, piszą wiadomości i pobierają pliki od Was.
- **Linki do pobrania.** Wysyłasz komuś np. grafikę 1 GB, a odbiorca pobiera ją prosto z NAS,
  bez logowania. Możesz ustawić termin ważności, limit pobrań i hasło.
- **BSK Backup.** Program na komputery pracowników. Każdy nowy lub zapisany plik trafia
  automatycznie na NAS, także po okresie bez internetu.

```
                 Internet                                     Biuro (NAS)
 klient ──► https://cloud.bskmedia.pl ──► router ──► Caddy (HTTPS) ──► portal BSK Cloud ──► SSD (baza) + HDD (pliki)
 pracownik (BSK Backup) ───────────────────────────────────────────────┘
```

## Gdzie to działa i dlaczego na NAS, a nie na Vercelu

Całość działa na Twoim NAS w Dockerze. **Vercel się tu nie nadaje**, bo:
- nie przechowuje plików, a funkcje Vercela przyjmują żądania do ok. 4,5 MB, więc pliku 1 GB nie da się przez niego wysłać;
- pliki i tak leżą na NAS, więc Vercel byłby zbędnym pośrednikiem: każdy plik szedłby dwa razy przez internet;
- potrzebne byłoby płatne zewnętrzne miejsce na pliki (S3 lub podobne), a wtedy NAS przestaje mieć sens.

Jedyne ograniczenie przy NAS to **prędkość wysyłania (upload) Twojego łącza w biurze**. Klient
pobiera plik 1 GB z prędkością, z jaką biuro wysyła dane do internetu. Przykład: przy 100 Mb/s
uploadu to ok. 1,5 minuty, przy 20 Mb/s ok. 7 minut.

## Gdzie leżą pliki na NAS

Portal korzysta z dwóch dysków:

```
SSD (szybki) — /volume1/bsk-cloud/            HDD (duży) — /volume2/bsk-dane/
├── (kod portalu, docker-compose.yml, .env)    ├── klienci/<login>/…
└── baza/bskcloud.db   baza danych portalu     ├── udostepnione/…
                                               ├── kopie-pracownikow/<komputer>/…
                                               ├── instalator/
                                               └── app/tmp/   pliki w trakcie wysyłki
```

Numer woluminu HDD (`volume2`, `volume3`…) sprawdzisz w UGOS: **Storage**. Ustawiasz go
w `.env` (`BSK_DATA_FOLDER`). Wszystkie pliki są w zwykłych folderach, widocznych też
w menedżerze plików UGOS i przez SMB:

```
/volume2/bsk-dane/
├── klienci/<login>/…                 pliki od klienta
│              └── od BSK Media/       pliki, które udostępniłeś klientowi w portalu
├── udostepnione/…                    pliki do wysyłania linkiem (możesz wrzucać tu przez SMB)
├── kopie-pracownikow/<komputer>/…    kopie z komputerów pracowników
│                     └── .wersje/     poprzednie wersje plików
└── instalator/                       program BSK Backup (pracownicy pobierają go z linku)
```

---

# Uruchomienie krok po kroku

## Krok 1. Przygotuj NAS (ok. 10 min)
1. W UGOS (**Files → Shared Folder → +**) utwórz dwa foldery współdzielone:
   - `bsk-cloud` na woluminie z **SSD** (portal i baza),
   - `bsk-dane` na woluminie z **HDD** (pliki).
   Jeśli na HDD nie ma jeszcze woluminu, utwórz go w **Storage**.
2. **App Center** → zainstaluj **Docker**.
3. **Panel sterowania → Terminal** → włącz **SSH**.
4. Ustaw NAS-owi **stały adres IP w sieci lokalnej**, np. `192.168.1.50`. Zrobisz to w ustawieniach
   sieci NAS albo przez rezerwację DHCP w routerze.

## Krok 2. Wgraj portal na NAS
Z komputera zaloguj się do NAS przez SSH (w Windows: PowerShell):
```bash
ssh twoj-login-nas@192.168.1.50
```
Na NAS pobierz kod i przygotuj konfigurację:
```bash
cd /volume1/bsk-cloud
sudo git clone https://github.com/BSK-Media/bsk-cloud.git portal
cd portal
sudo cp .env.example .env
openssl rand -hex 32
```
Jeśli na NAS nie ma `git` albo repozytorium jest prywatne, pobierz ZIP z GitHuba
(**Code → Download ZIP**) i rozpakuj go do `/volume1/bsk-cloud/portal` przez menedżer plików UGOS.

## Krok 3. Uzupełnij plik `.env`
```bash
ls -ln /volume1 /volume2
sudo nano .env
```
- `BSK_SECRET_KEY`: wklej wynik `openssl rand -hex 32` z kroku 2.
- `BSK_ADMIN_USERNAME` i `BSK_ADMIN_PASSWORD`: Twoje konto administratora. Hasło musi mieć
  min. 10 znaków, małe i wielkie litery oraz cyfrę.
- `BSK_DATA_FOLDER`: folder na HDD, np. `/volume2/bsk-dane`.
- `BSK_DB_FOLDER`: folder na bazę na SSD, np. `/volume1/bsk-cloud/baza`.
- `PUID` i `PGID`: liczby z polecenia `ls -ln` w wierszu folderu `bsk-dane`
  (kolumna 3 to UID, kolumna 4 to GID).
- Opcjonalnie: `BSK_DEFAULT_QUOTA_MB` (domyślny limit miejsca dla klienta) i `BSK_SMTP_*`
  (powiadomienia e-mail).

W edytorze nano zapisujesz przez Ctrl+O i Enter, wychodzisz przez Ctrl+X.

## Krok 4. Uruchom
```bash
sudo mkdir -p /volume1/bsk-cloud/baza && sudo chown PUID:PGID /volume1/bsk-cloud/baza
sudo docker compose up -d --build
sudo docker compose ps
```
W poleceniu `chown` wpisz liczby PUID i PGID z `.env`.
Oba kontenery (`bsk-cloud` i `bsk-caddy`) powinny mieć status *running*. W aplikacji Docker
w UGOS też je zobaczysz i stamtąd możesz je restartować oraz przeglądać logi.

## Krok 5. Podłącz domenę cloud.bskmedia.pl

**Wariant A (zalecany, jeśli masz publiczny adres IP).**
1. Sprawdź swój publiczny IP, np. na stronie *jakiejestmojeip*. Jeśli adres w routerze
   (WAN) jest inny niż ten ze strony, to masz CGNAT i musisz użyć wariantu B.
2. W panelu DNS domeny `bskmedia.pl` (u rejestratora) dodaj rekord:
   **typ A, nazwa `cloud`, wartość: Twój publiczny IP**.
   Jeśli IP jest zmienne, ustaw w routerze DDNS i zamiast rekordu A dodaj rekord CNAME na adres DDNS.
3. W routerze ustaw **przekierowanie portów (port forwarding)**:
   - port zewnętrzny **80** → `192.168.1.50`, port **8880**,
   - port zewnętrzny **443** → `192.168.1.50`, port **8443**.
4. Po kilku minutach od zmiany DNS otwórz **https://cloud.bskmedia.pl**. Certyfikat HTTPS
   (Let's Encrypt) pobierze się sam.

**Wariant B (Cloudflare Tunnel: bez otwierania portów, działa przy CGNAT).**
DNS domeny musi być w Cloudflare (plan darmowy wystarczy).
1. W Cloudflare przejdź do **Zero Trust → Networks → Tunnels → Create tunnel** i skopiuj token
   do `CLOUDFLARE_TUNNEL_TOKEN` w `.env`.
2. W tunelu dodaj *Public hostname*: `cloud.bskmedia.pl` → `http://app:8000`.
3. Uruchom: `sudo docker compose --profile tunnel up -d --build`.

Uwaga: w darmowym Cloudflare pojedyncze **wysłanie** pliku na serwer jest ograniczone do
100 MB. Pobieranie linkiem nie ma tego limitu. Jeśli klienci mają wysyłać duże pliki, wybierz wariant A.

## Krok 6. Pierwsze logowanie
1. Otwórz `https://cloud.bskmedia.pl` i zaloguj się kontem z `.env`.
2. Usuń hasło z pliku `.env`, bo konto już jest w bazie:
   `sudo nano .env`, wyczyść `BSK_ADMIN_PASSWORD=`, a potem uruchom `sudo docker compose up -d`.

## Krok 7. Program BSK Backup dla pracowników (raz)
Program dla Windows buduje się **automatycznie na GitHubie**, więc nie potrzebujesz Pythona:
1. Na GitHubie wejdź w repozytorium **Actions → „Testy i build BSK Backup”** i wybierz ostatnie
   zielone uruchomienie. W sekcji **Artifacts** pobierz `BSK-Backup-windows`.
   Inna droga: wypchnij tag `v1.0.0`, a gotowy ZIP pojawi się w **Releases**.
2. Rozpakuj ZIP i skopiuj 4 pliki (`bsk-backup.exe`, `bsk-backup-agent.exe`,
   `install-windows.ps1`, `uninstall-windows.ps1`) do folderu **`bsk-dane/instalator`** na NAS.
   Możesz to zrobić przez SMB albo menedżer plików UGOS.

Od teraz pracownicy pobierają program **bezpośrednio z NAS** przez link z portalu.

---

# Codzienna obsługa (panel administratora)

## Konta klientów
1. **+ Klient** → wpisz login, osobę kontaktową, firmę, e-mail i **limit miejsca** (MB, `0` = bez limitu).
2. Po zapisaniu zobaczysz **raz** adres, login i hasło. Przekaż je klientowi.
   Przy pierwszym logowaniu klient musi ustawić własne hasło.
3. Na stronie klienta możesz:
   - przeglądać jego pliki i wiadomości;
   - wysyłać mu pliki (trafiają do „od BSK Media”);
   - zmienić limit miejsca;
   - nadać nowe hasło;
   - zablokować konto (klient natychmiast traci dostęp).

**Limit miejsca** liczy tylko pliki wysłane przez klienta. To, co Ty mu udostępniasz, się nie
wlicza. Klient widzi pasek zajętego miejsca, a po przekroczeniu limitu wysyłka zostaje odrzucona.

## Linki do pobrania (np. grafika 1 GB)
Masz trzy sposoby:
- **Plik klienta:** na stronie klienta przy pliku kliknij **Link**. Powstaje link ważny 7 dni.
- **Wyślij i utwórz link:** w zakładce **Linki do pobrania** przeciągnij plik, ustaw ważność,
  limit pobrań i hasło.
- **Plik już na NAS (najlepsze dla dużych plików):** skopiuj plik w biurze przez sieć do folderu
  `bsk-dane/udostepnione`. Kopiowanie w sieci lokalnej jest najszybsze. Potem w **Linki do pobrania**
  wybierz plik z listy i kliknij **Utwórz link**.

Link ma postać `https://cloud.bskmedia.pl/s/Xy7…`. Odbiorca otwiera stronę z nazwą i rozmiarem
pliku i klika **Pobierz**. Plik idzie prosto z NAS, a przerwane pobieranie można wznowić.
W zakładce widzisz liczbę pobrań i możesz w każdej chwili **wyłączyć** link.

## Kopie komputerów pracowników
1. **+ Komputer** → nazwa (np. `LAPTOP-ANNA`), pracownik, opcjonalnie limit miejsca.
2. Portal pokaże **link instalacyjny** (ważny 7 dni, maks. 5 pobrań). Wyślij go pracownikowi.
3. Pracownik na swoim komputerze:
   1. otwiera link i klika **Pobierz BSK-Backup.zip**;
   2. rozpakowuje ZIP (prawy przycisk → *Wyodrębnij wszystkie*);
   3. klika dwukrotnie **ZAINSTALUJ.cmd**;
   4. jeśli pojawi się niebieskie okno SmartScreen, wybiera *Więcej informacji → Uruchom mimo to*.
      Program nie ma płatnego podpisu cyfrowego, stąd to ostrzeżenie.

   Nie potrzebuje uprawnień administratora ani wpisywania czegokolwiek, bo token jest już w paczce.
4. Program działa w tle, startuje sam po zalogowaniu i kopiuje **Pulpit** i **Dokumenty**
   (także te w OneDrive).
5. W portalu, na stronie komputera, widzisz wszystkie pliki z kopii, wyszukiwarkę, ostatni kontakt
   i poprzednie wersje każdego pliku do pobrania.

Jak działa program:
- reaguje na utworzenie, zapis, skopiowanie i przeniesienie pliku;
- odczekuje ok. 10 s, aż plik przestanie się zmieniać, i wysyła tylko pliki faktycznie zmienione;
- pomija pliki tymczasowe i systemowe (`~$*.docx`, `*.tmp`, `Thumbs.db`, `node_modules`, `.git` …);
- **bez internetu** pliki czekają w kolejce na dysku i wysyłają się same po powrocie połączenia,
  także po restarcie komputera;
- co godzinę skanuje foldery, więc nic nie umknie;
- usunięcie pliku na komputerze **nie** usuwa go z kopii, a nadpisanie zachowuje poprzednią wersję
  (domyślnie 10 wersji, ustawienie `BSK_BACKUP_KEEP_VERSIONS`);
- przy przekroczeniu limitu miejsca komputer wstrzymuje wysyłkę na godzinę i zapisuje to w dzienniku;
- **Nowy link instalacyjny** lub **Wyłącz urządzenie** natychmiast odcina stary token,
  np. po zwolnieniu pracownika, kradzieży laptopa albo reinstalacji.

Inne foldery niż Pulpit i Dokumenty ustawisz u pracownika poleceniem:
```powershell
& "$env:LOCALAPPDATA\Programs\BSKBackup\bsk-backup.exe" configure --folder "C:\Projekty" --folder "D:\Klienci"
```
Stan programu u pracownika:
```powershell
& "$env:LOCALAPPDATA\Programs\BSKBackup\bsk-backup.exe" status
```
Dziennik programu: `%LOCALAPPDATA%\BSKBackup\bsk-backup.log`.

## Dziennik
Zakładka **Dziennik** pokazuje logowania (także nieudane), wysłane i usunięte pliki, pobrania
linkami i instalatora oraz zmiany kont.

---

# Bezpieczeństwo i utrzymanie
- Ruch jest szyfrowany (HTTPS). Hasła są zapisane jako bcrypt, a po wielu błędnych logowaniach
  następuje blokada. Formularze mają ochronę CSRF, strona ma nagłówki bezpieczeństwa (CSP, HSTS).
  Linki do pobrania mają losowe adresy i nie są indeksowane przez wyszukiwarki.
- **RAID to nie kopia zapasowa.** W UGOS (*Kopia zapasowa i synchronizacja*) ustaw kopię folderów
  `bsk-dane` i `bsk-cloud/baza` na dysk USB i/lub do chmury, zgodnie z zasadą 3-2-1.
- Aktualizacja portalu:
  ```bash
  cd /volume1/bsk-cloud/portal && sudo git pull && sudo docker compose up -d --build
  ```
  Dane w `bsk-dane` i baza w `bsk-cloud/baza` pozostają nienaruszone.
- Aktualizacja BSK Backup: podmień pliki w `instalator/` i wyślij pracownikom nowe linki instalacyjne.
  Ponowna instalacja nadpisuje program i zachowuje kolejkę.

# Dla programisty
```bash
pip install -r server/requirements-dev.txt -r agent/requirements.txt
(cd server && python -m pytest -q)
(cd agent && python -m pytest -q)
```
Lokalny start portalu:
```bash
cd server
BSK_DATA_DIR=./dev-data BSK_SECRET_KEY=$(openssl rand -hex 32) BSK_COOKIE_SECURE=false \
BSK_ADMIN_USERNAME=admin BSK_ADMIN_PASSWORD=ZmienMnie123 BSK_PUBLIC_URL=http://127.0.0.1:8000 \
uvicorn app.main:create_app --factory --reload
```
Ręczne budowanie programu dla Windows: `agent\build-windows.bat` (wymaga Pythona 3.11+).
macOS/Linux: `python3 bsk_backup.py configure --server … --token …` + `agent/macos-launchagent.plist`.
