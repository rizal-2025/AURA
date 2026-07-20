# Changelog

Semua perubahan penting pada AURA dicatat di dokumen ini. Repository belum memiliki tag rilis, sehingga entri berikut mengikuti riwayat commit proyek.

## 2026-07-20 - Secure Customer Identity (V1.5 Phase 3)

### Added

- Validasi konfigurasi autentikasi saat startup: `AUTH_JWT_SECRET` wajib tersedia dan minimal 32 karakter, sedangkan `AUTH_JWT_EXPIRE_MINUTES` wajib berupa integer positif.
- Validasi defensif yang sama saat token dibuat, sehingga perubahan konfigurasi runtime yang tidak aman tidak dapat menerbitkan token.
- Regression test konfigurasi JWT, penolakan token tidak sah, isolasi ownership, record legacy tersembunyi, parser jumlah orang natural, serta verifikasi log AURA tidak berisi bearer token atau JWT secret.
- Dokumentasi konfigurasi lokal, migrasi aman, guest token, Swagger authorization, kebijakan record legacy, dan batasan identitas guest.

### Changed

- Dokumentasi ownership kini membedakan tegas `session_id` (memori percakapan) dari `owner_customer_id` (UUID pelanggan tervalidasi dari bearer token).
- Seluruh endpoint yang membuat atau mengelola reservasi (`POST /chat` dan `POST /reservation/`) memerlukan bearer token; Create, Read, Update, dan Cancel memakai `owner_customer_id` saja untuk ownership.

### Security notes

- Nilai secret maupun bearer token tidak ditulis oleh logger aplikasi AURA. Header `Authorization` tidak masuk ke custom state-transition logs.
- Tidak ada migrasi dijalankan pada fase ini; record lama tidak dihapus, diubah, atau di-backfill.

## 2026-07-20 - Secure Customer Identity (V1.5 Phases 1-2B)

### Added

- Tabel `customers`, JWT guest bearer token, dan dependency `get_current_customer` untuk memvalidasi signature, expiry, issuer, audience, status pelanggan, serta `token_version`.
- Kolom ownership aman `reservations.owner_customer_id` yang merujuk ke `customers.id`.
- Filter ownership dan operasi UPDATE/CANCEL atomik dengan predicate `id` dan `owner_customer_id` dari pelanggan terautentikasi.

### Notes

- Kolom `customer_id` V1.4 dipertahankan sebagai data legacy dan tidak lagi dipakai sebagai sumber ownership aman.
- Record dengan `owner_customer_id = NULL` tetap tersimpan, tetapi tidak ditampilkan atau dapat dikelola lewat fitur "reservasi saya".

## 2026-07-20 - Reservation Customer Ownership (V1.4)

### Added

- Kolom nullable `customer_id` pada model reservasi untuk menyimpan identitas sementara dari `ChatRequest.session_id`.
- Filter ownership pada daftar, pemilihan, update, dan pembatalan reservasi; ID milik pelanggan lain mendapat respons aman yang sama seperti ID tidak ditemukan.
- Pembentukan reservasi percakapan menyimpan `session_id` sebagai `customer_id`; endpoint `POST /reservation/` menggunakan header wajib `X-Session-ID` untuk tujuan yang sama.
- Skrip migrasi idempoten `migrations/add_customer_id_to_reservations.py` yang hanya menambahkan kolom bila belum ada.
- Regression test untuk ownership saat create, isolasi dua session, filter Read, penolakan Update/Cancel lintas pelanggan, dan record legacy `NULL`.

### Migration

Jalankan sekali dari root project:

```powershell
.\.venv\Scripts\python.exe migrations\add_customer_id_to_reservations.py
```

Skrip memverifikasi tabel `reservations` sudah ada, lalu memakai `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`. Skrip tidak membuat ulang tabel, menghapus record, atau mem-backfill data lama.

### Notes

- Record lama tetap memiliki `customer_id = NULL` dan diperlakukan sebagai record legacy: tidak ditampilkan melalui `reservasi saya`, tetapi tidak dihapus.
- `session_id` adalah identitas sementara untuk V1.4, bukan pengganti autentikasi pelanggan permanen.

## 2026-07-20 - Reservation Management (CANCEL)

### Added

- Intent `cancel_reservation` dan `CancelReservationAgent` untuk membatalkan reservasi tersimpan melalui percakapan.
- Alur daftar lima reservasi terbaru, pemilihan ID, ringkasan, dan konfirmasi **Ya/Tidak** sebelum pembatalan.
- Operasi repository/service yang mengubah status reservasi menjadi `cancelled` tanpa menghapus record.
- Regression test untuk pembatalan sukses, ID tidak valid, penolakan pengguna, record yang sudah dibatalkan, state lintas pesan, dan kompatibilitas confirmation flow.

### Notes

- Batas daftar global ini digantikan oleh filter ownership berbasis `customer_id` pada V1.4.
- V1.3 tidak mengubah schema database, API `MemoryManager`, atau alur Create, Read, dan Update yang sudah ada.

## 2026-07-19 - Reservation Management (UPDATE)

### Added

- Intent `update_reservation` dan `UpdateReservationAgent` untuk mengubah reservasi yang telah tersimpan.
- Alur pilih ID reservasi, pilih field, masukkan nilai baru, lalu tampilkan ringkasan record yang diperbarui.
- Operasi repository/service untuk membaca record berdasarkan ID dan melakukan UPDATE pada field `name`, `people`, `date`, atau `time`.
- Regression test untuk update sukses, ID/field tidak valid, serta pembaruan people, date, dan time.

### Notes

- Update memakai session memory yang sudah ada tanpa mengubah API `MemoryManager`, confirmation flow, atau schema database.

## 2026-07-19 - Reservation Management (READ)

### Added

- Intent `view_reservation` untuk permintaan daftar reservasi, termasuk frasa Indonesia dan Inggris yang umum.
- Handler baca khusus yang mengambil maksimal lima record terbaru dari PostgreSQL dengan urutan `id DESC`.
- Format respons berisi ID, nama, jumlah orang, tanggal, jam, dan status reservasi.
- Regression test untuk routing intent, urutan/limit query, format respons, dan kondisi data kosong.

### Notes

- Mulai V1.4, daftar V1.1 difilter berdasarkan `customer_id` dari session aktif.

## 2026-07-18 - Complete Reservation V1 workflow

### Added

- Alur reservasi percakapan multi-turn dengan pengumpulan `name`, `people`, `date`, dan `time`.
- Orchestrator, planner, workflow, dan agent reservasi untuk menjalankan alur chat.
- State percakapan per sesi, pemisahan sesi, dan pelacakan field yang sudah ditanyakan.
- Ekstraksi entitas reservasi sederhana dari pesan Indonesia, resolver konteks untuk perubahan data, serta parser tanggal/waktu.
- Tahap konfirmasi reservasi dengan jawaban **Ya/Tidak** dan nomor reservasi pada sesi setelah konfirmasi berhasil.
- Memori preferensi pengguna jangka panjang berbasis in-memory.
- Dukungan intent dengan confidence score pada classifier utama.
- Kerangka tool async: `ToolManager`, antarmuka tool, dan `DatabaseTool` contoh.
- Logging aplikasi dan dokumentasi struktur proyek.
- Test untuk workflow, classifier, planner, state percakapan, memori, extractor, konfirmasi, router, tool, provider AI, dan parser tanggal/waktu.

### Changed

- Endpoint chat menggunakan orchestrator dan workflow Reservation V1.
- Factory AI memilih provider Ollama atau OpenAI dari konfigurasi aplikasi.
- Ekstraksi reservasi menambahkan normalisasi tanggal dan waktu lokal.

## 2026-07-14 - Reservation database persistence

### Added

- Model SQLAlchemy `Reservation` beserta tabel `reservations`.
- Konfigurasi engine, session database, repository, dan service reservasi.
- Endpoint `POST /reservation/` untuk membuat reservasi secara langsung.
- Schema Pydantic untuk request/response chat dan reservasi.
- Provider AI OpenAI dan Ollama, classifier intent, serta extractor reservasi berbasis AI.
- Script pembuatan tabel dan smoke test database awal.

## 2026-07-13 - Initialize AURA FastAPI project

### Added

- Inisialisasi aplikasi FastAPI AURA.
- Konfigurasi aplikasi berbasis environment.
- Endpoint `GET /` dan `GET /health`.
- Daftar dependency awal proyek.
