# AURA

AURA adalah API FastAPI untuk reservasi restoran berbasis percakapan. V1.5 memakai identitas pelanggan guest yang divalidasi server untuk menjaga ownership reservasi.

## Konfigurasi lokal

Salin `.env.example` menjadi `.env`, lalu isi nilai lokal yang aman. Variabel minimum:

- `DATABASE_URL`
- `AUTH_JWT_SECRET` — minimal 32 karakter acak.
- `AUTH_JWT_ISSUER`, `AUTH_JWT_AUDIENCE`, dan `AUTH_JWT_EXPIRE_MINUTES` (integer ketat antara 1 dan 1440).
- `SQL_ECHO=false` untuk menonaktifkan log SQL dan nilai query secara default. Jangan aktifkan pada lingkungan yang memproses data pelanggan tanpa kontrol log yang memadai.

Contoh pembuatan secret lokal (jangan kirim atau commit hasilnya):

```powershell
.\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(32))"
```

Konfigurasi JWT yang tidak valid membuat aplikasi gagal memulai dengan pesan konfigurasi yang aman; nilai secret tidak dicetak. Nilai expiry boolean, float, string desimal, nol, negatif, atau lebih dari 1440 ditolak.

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

Tiket tidak menyimpan raw `session_id`, composite memory key, bearer token, secret, Authorization header, transcript, pesan mentah, nama pelanggan, atau detail tanggal/jam reservasi. Notifikasi Telegram belum tersedia.

## Telegram customer bot (V1.7 Phase D)

Telegram memakai long polling lokal sebagai proses terpisah; FastAPI tidak memulai poller dan tetap dapat berjalan tanpa konfigurasi Telegram. Isi `TELEGRAM_BOT_TOKEN` dan `TELEGRAM_IDENTITY_SECRET` hanya di `.env` lokal. Secret identity harus acak, stabil, minimal 32 karakter non-whitespace, dan bebas newline, tab, null, atau karakter kontrol. Seluruh konfigurasi Telegram divalidasi ketika runner dimulai.

Gunakan secret berbeda dari JWT secret. Contoh pembuatan secret lokal:

```powershell
.\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(48))"
```

Mengubah secret atau versi derivasi HMAC memutus mapping Telegram yang sudah ada. Identity memakai domain `aura:telegram:identity:v1`, sedangkan percakapan private memakai domain `aura:telegram:private-session:v1`.

Jalankan migrasi identitas secara manual setelah meninjau database, kemudian mulai proses terpisah:

```powershell
.\.venv\Scripts\python.exe migrations\add_telegram_identities.py
.\.venv\Scripts\python.exe -m app.integrations.telegram.runner
```

Jangan menjalankan migrasi otomatis saat startup. Bot hanya menerima private chat, dengan `/start`, `/help`, dan `/status`; gambar, file, voice note, kontak, dan lokasi tidak diteruskan ke AURA. Hanya satu polling instance yang didukung untuk demo ini.

Identitas Telegram tidak memakai bearer token: AURA membuat atau memakai `Customer` server-side dari HMAC-SHA256 atas Telegram user ID. User ID/chat ID mentah, username, dan token tidak disimpan. Referensi percakapan juga berupa HMAC internal, sehingga workflow, ownership, handoff, dan ticket tetap terisolasi.

Sebelum polling, runner memeriksa webhook aktif. Default-nya runner berhenti aman. Untuk penghapusan webhook yang disengaja saja, set `TELEGRAM_CLEAR_WEBHOOK_ON_START=true`; `TELEGRAM_DROP_PENDING_UPDATES=false` mempertahankan update tertunda. `/status` memeriksa tiket aktif secara customer-session scoped tanpa AI atau classifier. Phase D belum mengirim notifikasi owner melalui Telegram.

Logger pihak ketiga `httpx`, `httpcore`, dan Telegram dibatasi agar URL Bot API tidak mencatat token. Filter redaction tetap diterapkan sebagai lapisan kedua. Jika token pernah muncul di log, segera rotasi melalui BotFather dan perlakukan seluruh salinan log sebagai data sensitif.

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

Unit tests, tanpa membutuhkan PostgreSQL eksternal:

```powershell
$modules = Get-ChildItem tests -File -Filter 'test_*.py' | ForEach-Object { "tests.$($_.BaseName)" }
.\.venv\Scripts\python.exe -m unittest $modules -v
```

PostgreSQL integration tests bersifat opt-in. Gunakan database disposable khusus yang berbeda dari `DATABASE_URL`; nama databasenya harus mengandung `test`:

```powershell
$env:TEST_DATABASE_URL = "postgresql+psycopg://TEST_USERNAME:TEST_PASSWORD@localhost:5432/aura_test"
.\.venv\Scripts\python.exe -m unittest discover -s tests\integration -v
```

Jika `TEST_DATABASE_URL` tidak tersedia, integration tests dilewati dengan alasan yang jelas. Test membuat schema unik dan hanya membersihkan schema tersebut.
