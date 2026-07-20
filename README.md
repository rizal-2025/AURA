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

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```
