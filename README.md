# AURA

AURA adalah API FastAPI untuk reservasi restoran berbasis percakapan. V1.5 memakai identitas pelanggan guest yang divalidasi server untuk menjaga ownership reservasi.

## Konfigurasi lokal

Salin `.env.example` menjadi `.env`, lalu isi nilai lokal yang aman. Variabel minimum:

- `APP_ENV` — wajib dan harus persis `development`, `test`, `staging`, atau `production`; nilai tidak di-trim dan tidak diubah case.
- `DATABASE_URL`
- `AUTH_JWT_SECRET` — 32–512 karakter acak, tanpa whitespace luar, control character, placeholder, atau pola pengulangan trivial.
- `AUTH_JWT_ISSUER`, `AUTH_JWT_AUDIENCE`, dan `AUTH_JWT_EXPIRE_MINUTES` (integer ketat antara 1 dan 1440).
- `SQL_ECHO=false` untuk menonaktifkan log SQL dan nilai query secara default. Jangan aktifkan pada lingkungan yang memproses data pelanggan tanpa kontrol log yang memadai.

Contoh pembuatan secret lokal (jangan kirim atau commit hasilnya):

```powershell
.\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(32))"
```

Konfigurasi JWT yang tidak valid membuat aplikasi gagal memulai dengan kode aman
`CFG_AUTH_*`; nilai secret tidak dicetak. Nilai expiry boolean, float, string
desimal, nol, negatif, atau lebih dari 1440 ditolak. Staging dan production
menolak issuer/audience development `aura` dan `aura-api`, sehingga keduanya
harus diisi eksplisit untuk deployment.

Konfigurasi dibatasi per proses:

- FastAPI memuat environment, database, JWT, dan provider AI; variabel Telegram tidak dibutuhkan.
- Runner Telegram memuat environment, database, provider AI, serta konfigurasi runner Telegram.
- Migrasi manual memuat environment dan database saja.

`AI_PROVIDER` menerima persis `ollama` atau `openai`. Nilai lain menghentikan
startup dan tidak fallback ke Ollama. OpenAI membutuhkan API key valid ketika
dipilih dan tidak lagi menggunakan dummy key. Pada staging/production, URL
Ollama HTTP hanya diizinkan untuk loopback; endpoint remote harus memakai HTTPS.
Inisialisasi provider tidak melakukan request jaringan.

Kode kegagalan konfigurasi yang aman meliputi `CFG_ENV_INVALID`,
`CFG_AUTH_SECRET_INVALID`, `CFG_AUTH_EXPIRY_INVALID`,
`CFG_AUTH_ISSUER_INVALID`, `CFG_AUTH_AUDIENCE_INVALID`,
`CFG_AI_PROVIDER_INVALID`, `CFG_AI_OPENAI_INVALID`, dan
`CFG_AI_OLLAMA_INVALID`. Runner memakai kode `CFG_TELEGRAM_*`. Kode tidak
menyertakan raw environment value, token, URL, ID, atau secret.

## Serialisasi percakapan V2.0 G1C

Setiap operasi chat terautentikasi memakai lock async in-process berdasarkan
scope internal yang sama dengan memory percakapan: authenticated customer dan
session reference tervalidasi. Dua pesan untuk percakapan yang sama diproses
bergantian dari handoff recovery sampai workflow, database/ticket/outbox, dan
mutasi memory selesai. Percakapan dengan customer atau session berbeda tetap
dapat berjalan bersamaan.

Waktu tunggu lock dibatasi 15 detik. HTTP mengembalikan status `409` dengan kode
aman `CONVERSATION_BUSY`; Telegram menjawab
`Pesan sebelumnya masih diproses. Silakan coba lagi sebentar.` Lock key,
customer UUID, session reference, dan input mentah tidak dimasukkan ke respons
atau log.

G1C hanya bekerja di dalam satu proses Python. Deployment saat ini wajib memakai
satu FastAPI/Uvicorn worker dan satu proses Telegram polling. Runner Telegram
memproses maksimal delapan update secara bersamaan, lalu keyed lock memastikan
percakapan yang sama tetap serial. FastAPI dan Telegram yang berjalan sebagai
proses OS terpisah tidak berbagi lock; satu percakapan logis harus diarahkan
konsisten melalui satu ingress process. Multiple FastAPI instance, distributed
locking, Redis, advisory lock, dan queue belum didukung.

Lock ini tidak menyediakan rollback memory atau transaction ownership. Database
transaction hardening tetap menjadi pekerjaan V2.0 G1D. G1C tidak menambah
schema atau migration.

Perintah tiket owner dan pemrosesan percakapan customer tidak memakai lock
percakapan in-process yang sama. Transisi tiket oleh owner tetap dilindungi row
lock database, tetapi resolve yang berjalan bersamaan dengan pemulihan handoff
customer dapat menghasilkan satu respons lama bahwa percakapan masih menunggu
petugas. Pesan customer berikutnya merekonsiliasi status terminal dan melepas
handoff state yang sudah tidak aktif. Koordinasi lifecycle/transaction secara
penuh tetap scope G1D; keterbatasan ini tidak diperbaiki atau diklaim selesai
oleh G1C dan tidak memerlukan migration schema.

## Migrasi V1.5

Jalankan sekali pada database yang sudah memiliki tabel `reservations`:

```powershell
.\.venv\Scripts\python.exe migrations\add_secure_customer_identity.py
```

Migrasi idempoten ini menambahkan fondasi `customers` dan `owner_customer_id` bila belum ada. Migrasi tidak membuat ulang tabel reservasi, menghapus record, atau melakukan backfill data legacy. Jangan jalankan ulang migrasi sebagai bagian dari startup aplikasi.

## Support ticket dan migrasi V1.6

Handoff membuat tiket persisten dengan status aktif `open` atau `in_progress`. Untuk satu pelanggan terautentikasi dan satu referensi percakapan hanya boleh ada satu tiket aktif. Tiket `resolved` atau `closed` tidak digunakan kembali; handoff berikutnya dapat memperoleh nomor tiket baru.

Migrasi support ticket tetap manual dan tidak dijalankan saat startup:

```powershell
.\.venv\Scripts\python.exe migrations\add_support_tickets.py
```

Migrasi memeriksa kolom, foreign key, CHECK constraint, uniqueness, dan index yang diperlukan. Migrasi tidak menghapus atau mengubah record reservasi. Jika tabel support ticket lama mengandung nilai priority/status yang tidak valid, migrasi berhenti aman agar data dapat ditinjau secara manual.

Pada awal `POST /chat`, AURA mencari tiket aktif berdasarkan authenticated customer dan hash internal customer-session ketika lock belum ada di memory. Tiket aktif mengembalikan lock sebelum classifier, AI, Update, atau Cancel berjalan. State yang dipulihkan hanya berisi metadata operasional aman dan nomor tiket.

Tiket tidak menyimpan raw `session_id`, composite memory key, bearer token, secret, Authorization header, transcript, pesan mentah, nama pelanggan, atau detail tanggal/jam reservasi.

## Telegram customer bot (V1.7 Phase D)

Telegram memakai long polling lokal sebagai proses terpisah; FastAPI tidak memulai poller dan tetap dapat berjalan tanpa konfigurasi Telegram. Isi `TELEGRAM_BOT_TOKEN` dan `TELEGRAM_IDENTITY_SECRET` hanya di `.env` lokal. Secret identity harus acak, stabil, minimal 32 karakter non-whitespace, dan bebas newline, tab, null, atau karakter kontrol. Seluruh konfigurasi Telegram divalidasi ketika runner dimulai.

Gunakan secret berbeda dari JWT secret. Contoh pembuatan secret lokal:

```powershell
.\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(48))"
```

Mengubah secret atau versi derivasi HMAC memutus mapping Telegram yang sudah ada. Identity memakai domain `aura:telegram:identity:v1`, sedangkan percakapan private memakai domain `aura:telegram:private-session:v1`.

Jalankan migrasi identitas dan outbox secara manual setelah meninjau database, kemudian mulai proses terpisah:

```powershell
.\.venv\Scripts\python.exe migrations\add_telegram_identities.py
.\.venv\Scripts\python.exe migrations\add_support_ticket_notifications.py
.\.venv\Scripts\python.exe -m app.integrations.telegram.runner
```

Jangan menjalankan migrasi otomatis saat startup. Bot hanya menerima private chat, dengan `/start`, `/help`, dan `/status`; gambar, file, voice note, kontak, dan lokasi tidak diteruskan ke AURA. Hanya satu polling instance yang didukung untuk demo ini. Di dalam proses tersebut, maksimal delapan update ditangani bersamaan dan pesan customer-session yang sama diserialisasi oleh keyed conversation lock.

Identitas Telegram tidak memakai bearer token: AURA membuat atau memakai `Customer` server-side dari HMAC-SHA256 atas Telegram user ID. User ID/chat ID mentah, username, dan token tidak disimpan. Referensi percakapan juga berupa HMAC internal, sehingga workflow, ownership, handoff, dan ticket tetap terisolasi.

Sebelum polling, runner memeriksa webhook aktif. Default-nya runner berhenti aman. Untuk penghapusan webhook yang disengaja saja, set `TELEGRAM_CLEAR_WEBHOOK_ON_START=true`; `TELEGRAM_DROP_PENDING_UPDATES=false` mempertahankan update tertunda. `/status` memeriksa tiket aktif secara customer-session scoped tanpa AI atau classifier.

## Notifikasi owner Telegram (V1.8 Phase E)

Phase E membuat satu job outbox `telegram_owner` dalam transaksi yang sama dengan tiket support baru. Tiket yang dipakai ulang, pesan selama automation lock, `/status`, resolve, dan close tidak membuat job baru. Isi pesan dirender saat dispatch hanya dari nomor, kategori, prioritas, status, ringkasan allowlisted, dan waktu tiket; owner chat ID, raw pesan, identitas pelanggan, session reference, token, dan secret tidak disimpan.

Konfigurasi ini hanya divalidasi oleh runner Telegram; FastAPI tetap tidak bergantung padanya:

- `TELEGRAM_OWNER_NOTIFICATIONS_ENABLED=false` — boolean ketat.
- `TELEGRAM_OWNER_CHAT_ID` — wajib hanya saat enabled; integer positif private ID.
- `TELEGRAM_OWNER_NOTIFICATION_POLL_SECONDS=5` — integer 1–300.
- `TELEGRAM_OWNER_NOTIFICATION_MAX_ATTEMPTS=5` — integer 1–20.
- `TELEGRAM_OWNER_NOTIFICATION_RETRY_BASE_SECONDS=10` — integer 1–3600.
- `TELEGRAM_OWNER_NOTIFICATION_LEASE_SECONDS=60` — integer 5–3600.

Dispatcher berjalan sebagai satu task sequential milik proses polling, setelah pemeriksaan webhook berhasil. Job diklaim dengan `FOR UPDATE SKIP LOCKED`, status `sending`, dan lease yang sudah dikomit sebelum request jaringan. Failure retryable memakai exponential backoff terbatas; failure permanen atau batas attempt mengubah status menjadi `failed`. Hanya error code allowlisted yang disimpan.

Telegram `sendMessage` tidak menyediakan idempotency key umum. Crash setelah Telegram menerima pesan tetapi sebelum AURA menandai job `sent` dapat menyebabkan satu pengiriman ulang setelah lease kedaluwarsa. Stable ticket number, deterministic outbox identity, dan lease mencegah duplikasi pada operasi normal, tetapi Phase E tidak mengklaim exactly-once delivery eksternal.

Phase E tidak mengirim notifikasi status tiket kepada pelanggan. Webhook deployment dan multi-runner deployment juga belum tersedia. Jangan menjalankan lebih dari satu polling instance pada demo ini.

UAT lokal Phase E yang aman dilakukan setelah test PostgreSQL disposable lulus dan migrasi normal ditinjau/di-backup:

1. Gunakan bot pengembangan dan private owner chat khusus; jangan gunakan token produksi di command history atau dokumentasi.
2. Jalankan migrasi outbox manual satu kali, lalu set konfigurasi owner hanya di `.env` lokal dan aktifkan notifikasi.
3. Mulai satu runner, picu satu handoff baru, dan pastikan satu pesan owner memuat nomor tiket yang sama tanpa identitas/detail reservasi.
4. Kirim pesan lanjutan selama automation lock dan jalankan `/status`; pastikan tidak ada notifikasi kedua.
5. Uji recipient invalid atau failure jaringan dengan bot test; tiket customer harus tetap ada sementara job mengikuti retry/failed lifecycle.
6. Matikan kembali feature flag setelah UAT. Jika bot token bocor, rotasi melalui BotFather sebelum runner dipakai lagi.

Migrasi tidak membuat job untuk tiket historis. Failure dispatch tidak menghapus, menutup, atau menyembunyikan tiket support yang sudah tersimpan.

Logger pihak ketiga `httpx`, `httpcore`, dan Telegram dibatasi agar URL Bot API tidak mencatat token. Filter redaction tetap diterapkan sebagai lapisan kedua. Jika token pernah muncul di log, segera rotasi melalui BotFather dan perlakukan seluruh salinan log sebagai data sensitif.

## Pengelolaan tiket oleh owner Telegram (V1.9 Phase F)

Phase F menyediakan command private-chat yang dinonaktifkan secara default:

- `/tickets` menampilkan maksimal 10 tiket aktif (`open`/`in_progress`) dari yang paling lama.
- `/ticket <ticket_number>` menampilkan detail allowlisted untuk tiket aktif maupun terminal.
- `/take <ticket_number>` menjalankan transisi `open` ke `in_progress`.
- `/resolve <ticket_number>` menjalankan transisi `open` atau `in_progress` ke `resolved`.

Aktifkan dengan `TELEGRAM_OWNER_COMMANDS_ENABLED=true` dan isi `TELEGRAM_OWNER_CHAT_ID` hanya di `.env` lokal. Flag command dan `TELEGRAM_OWNER_NOTIFICATIONS_ENABLED` independen; salah satu atau keduanya dapat diaktifkan. Runner memvalidasi ID sebagai integer positif. Handler mensyaratkan private chat serta kesamaan `effective_chat.id` dan `effective_user.id`; ID tidak disimpan, dicatat, atau diterima dari argumen command.

Command berulang bersifat deterministik dan tidak membuka kembali tiket terminal. Command owner tidak membuat job outbox "tiket baru" dan tidak mengklaim pelanggan telah diberi notifikasi. Customer status notification belum tersedia. Setelah `/resolve`, pesan pelanggan berikutnya merekonsiliasi lock dengan status PostgreSQL; hanya state handoff customer-session terkait yang dilepas.

Phase F tidak menambahkan migrasi. Runner tetap mendukung satu polling instance saja. UAT aman dilakukan dengan bot/database pengembangan setelah unit test dan PostgreSQL test disposable lulus: aktifkan flag command, verifikasi akun non-owner selalu mendapat `Perintah tidak tersedia.`, lalu uji `/tickets`, `/ticket`, `/take`, dan `/resolve` tanpa memasukkan ID/token ke log atau dokumentasi. Bila bot token pernah bocor, rotasi melalui BotFather sebelum UAT.

Untuk uji PostgreSQL integrasi, gunakan `TEST_DATABASE_URL` terpisah yang nama databasenya mengandung `test`; jangan pernah menggunakan `DATABASE_URL` utama. Tidak ada request ke Telegram nyata di test otomatis.

## Autentikasi guest dan penggunaan API

Ambil token guest dari server; klien tidak mengirim customer ID:

```powershell
$guest = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/auth/guest
$headers = @{ Authorization = "Bearer $($guest.access_token)" }
```

Gunakan `$headers` pada `POST /chat` dan `POST /reservation/`. Pada Swagger di `/docs`, klik **Authorize**, masukkan `access_token` yang diterima (Swagger menambahkan skema Bearer), lalu jalankan endpoint yang memerlukan autentikasi.

Contoh UAT lokal yang aman:

1. Ambil dua guest token berbeda.
2. Buat reservasi dengan token pertama, lalu lihat reservasi dengan kedua token untuk memverifikasi isolasi.
3. Coba Update atau Cancel ID milik token pertama memakai token kedua; API harus mengembalikan respons aman tanpa perubahan data.
4. Gunakan token pertama dengan dua `session_id` berbeda; owner reservasi tetap sama, sementara memori percakapan terpisah.

Jangan menaruh bearer token, JWT secret, API key, atau password database di source, dokumentasi, atau log.

## Model identitas dan legacy data

`session_id` dari `ChatRequest` hanya digunakan `MemoryManager` untuk state percakapan. Ownership aman selalu berasal dari token bearer yang divalidasi dependency `get_current_customer`, lalu disimpan sebagai `reservations.owner_customer_id`. Header `X-Session-ID` dan request body tidak dapat menetapkan owner. Operasi reservasi secure menolak owner yang tidak tersedia sebelum menjalankan query, sehingga `NULL` legacy tidak dapat menjadi fallback ownership.

Kolom `customer_id` V1.4 tetap dipertahankan sebagai legacy. Reservasi dengan `owner_customer_id = NULL` tidak dihapus ataupun di-backfill, namun tidak ditampilkan oleh Read dan tidak dapat dipilih Update/Cancel secara otomatis.

Pembuatan reservasi baru selalu memerlukan `owner_customer_id` dari bearer terautentikasi. Skrip insert legacy lama telah dinonaktifkan untuk mencegah record baru tanpa owner secure.

## Batasan saat ini

- Identity masih berupa guest identity, bukan akun pengguna permanen.
- Belum ada refresh token, registrasi/login berbasis password, atau account recovery.
- Token guest harus diperlakukan sebagai kredensial rahasia oleh klien.

## Verifikasi

Semua proses test harus menerima `APP_ENV=test` secara eksplisit dari command
environment; tidak ada bootstrap environment tersembunyi di `tests/__init__.py`.
Contoh konfigurasi offline yang aman:

```powershell
$env:APP_ENV = "test"
$env:DATABASE_URL = "sqlite://"
$env:SQL_ECHO = "false"
$env:AUTH_JWT_SECRET = "aura-unit-test-jwt-secret-safe-material-2026"
$env:AUTH_JWT_ISSUER = "aura"
$env:AUTH_JWT_AUDIENCE = "aura-api"
$env:AUTH_JWT_EXPIRE_MINUTES = "60"
$env:AI_PROVIDER = "ollama"
$env:OLLAMA_BASE_URL = "http://localhost:11434/v1"
$env:OLLAMA_MODEL = "aura-test-model"
```

Test top-level saja (tidak termasuk `tests/integration`):

```powershell
$modules = Get-ChildItem tests -File -Filter 'test_*.py' | ForEach-Object { "tests.$($_.BaseName)" }
.\.venv\Scripts\python.exe -m unittest $modules -v
```

Complete repository discovery, termasuk integration tests:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v
```

Tanpa `TEST_DATABASE_URL`, seluruh PostgreSQL integration tests ditemukan tetapi
ditandai skipped. Untuk benar-benar menjalankannya, gunakan database disposable
khusus yang berbeda dari `DATABASE_URL`; nama databasenya harus mengandung
`test`:

```powershell
$env:TEST_DATABASE_URL = "postgresql+psycopg://TEST_USERNAME:TEST_PASSWORD@localhost:5432/aura_test"
.\.venv\Scripts\python.exe -m unittest discover -s tests\integration -v
```

Test Phase F saja dapat dijalankan dengan:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.integration.test_telegram_owner_commands_postgresql -v
```

Jika `TEST_DATABASE_URL` tidak tersedia, integration tests dilewati dengan alasan yang jelas. Test membuat schema unik dan hanya membersihkan schema tersebut.

## AURA V2.0 Phase G1

Phase G1A environment/configuration hardening dan G1B input/HTTP body bounds
sudah selesai. Keduanya tidak memerlukan migrasi. G1C (serialisasi percakapan
customer-session) dan G1D (transaction ownership serta handoff recovery)
masih pending.

### Batas input G1B

- `POST /chat` menerima `session_id` sepanjang 1–128 karakter. Karakter pertama
  harus huruf/digit ASCII; karakter berikutnya hanya huruf/digit ASCII, `.`,
  `_`, atau `-`. Nilai tidak di-trim atau dinormalisasi.
- Pesan chat harus berisi 1–4096 Unicode code point setelah line ending
  `CRLF`/`CR` dinormalisasi menjadi `LF`. Pesan kosong/all-whitespace, NUL,
  control character selain `LF`, bidi control, dan zero-width format character
  ditolak. Teks Indonesia, emoji, tanda baca, dan line break normal tetap
  diterima.
- Nama reservasi dinormalisasi ke Unicode NFC, spasi ASCII di tepi dihapus,
  dan rangkaian spasi ASCII diringkas. Panjangnya 1–100 karakter dan hanya
  menerima huruf, mark, digit, spasi, apostrof, tanda hubung, titik, serta `&`.
  Nama harus mengandung setidaknya satu huruf atau digit. Extractor percakapan
  memisahkan koma/penanda kalimat dari clause reservasi sebelum validator,
  sehingga contoh `atas nama Rizal, untuk 4 orang` tetap menghasilkan `Rizal`
  tanpa mengizinkan koma sebagai bagian nama tersimpan.
- `people` adalah integer ketat 1–20; boolean, float, string angka, nol, dan
  nilai di luar batas ditolak.
- Endpoint reservasi langsung mensyaratkan tanggal nyata kanonis `YYYY-MM-DD`
  dan waktu `HH:MM` 24 jam. G1B tidak menolak tanggal lampau: validasi ini
  sengaja bersifat sintaksis dan tidak bergantung timezone host. Kebijakan
  availability/tanggal lampau tetap pekerjaan bisnis berikutnya. Tanggal
  relatif percakapan memakai UTC+7 sebagai timezone bisnis AURA yang eksplisit,
  bukan timezone lokal host.
- Field JSON tambahan ditolak. Ownership dan status lifecycle tetap tidak
  dapat ditentukan pemanggil.
- Validator yang sama dipakai pada boundary HTTP, shared authenticated chat,
  Telegram, Create, dan Update sehingga jalur non-HTTP tidak dapat melewati
  aturan input.

Semua HTTP request body dibatasi maksimum 16.384 byte. `Content-Length` yang
terlalu besar ditolak sebelum body dibaca; body tanpa panjang/chunked tetap
dibaca secara terbatas hingga satu byte di atas batas. Framing panjang yang
malformed, konflik, atau tidak cocok ditolak dengan respons generik.
Setiap representasi duplicate/comma-combined `Content-Length` harus identik
secara tekstual (`3,3` diterima; `3,03` ditolak). Satu nilai dengan leading
zero, misalnya `03`, tetap diterima sebagai panjang 3, tetapi tidak dianggap
ekuivalen dengan `3` pada duplicate header. Maksimum 1.024 frame body mencegah
loop tanpa batas dari frame kosong.

Sebelum repository Create dipanggil, service membangun mapping baru yang hanya
berisi `name`, `people`, `date`, dan `time`, lalu memvalidasinya melalui schema
baru. Instance Pydantic yang dimutasi atau dibuat dengan `model_construct()`
tidak dapat melewati batas ini; ownership tetap berasal dari parameter
terautentikasi yang terpisah.

Respons body terlalu besar:

```json
{"code":"REQUEST_BODY_TOO_LARGE","detail":"Request body is too large."}
```

Respons validasi `422` hanya memuat kode stabil dan lokasi field, tanpa nilai
input mentah. Kode field meliputi `CHAT_SESSION_ID_INVALID`,
`CHAT_MESSAGE_EMPTY`, `CHAT_MESSAGE_TOO_LONG`, `CHAT_MESSAGE_UNSAFE`,
`RESERVATION_NAME_INVALID`, `RESERVATION_PEOPLE_INVALID`,
`RESERVATION_DATE_INVALID`, `RESERVATION_TIME_INVALID`, dan
`EXTRA_FIELD_FORBIDDEN`. Respons autentikasi `401` tetap generik seperti
sebelumnya. Reverse proxy tetap harus menerapkan batas body yang sama saat
deployment; hardening proxy berada di luar scope G1B.
