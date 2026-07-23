# Changelog

Semua perubahan penting pada AURA dicatat di dokumen ini. Repository belum memiliki tag rilis, sehingga entri berikut mengikuti riwayat commit proyek.

## 2026-07-22 - Telegram Owner Ticket Management (V1.9 Phase F)

### Added

- Command owner private-chat `/tickets`, `/ticket`, `/take`, dan `/resolve` dengan konfigurasi runner yang nonaktif secara default.
- Otorisasi fail-closed yang memeriksa message, private chat, user/chat ID yang sama, serta menolak sender/forwarded/channel context sebelum parsing dan akses database.
- Read model immutable berisi hanya nomor tiket, kategori, prioritas, status, dan timestamp; renderer plain text Indonesia memakai chunk maksimum 4096 karakter.
- Transisi tiket ter-lock berdasarkan public ticket number dengan hasil idempoten dan rollback aman.
- Unit test offline serta PostgreSQL integration test opt-in untuk authorization, routing, privacy, concurrency, lock release, dan isolasi outbox.

### Fixed

- Lock handoff dalam memory sekarang direkonsiliasi dengan tiket aktif PostgreSQL pada pesan berikutnya. Tiket resolved/closed melepas hanya state handoff terkait tanpa menghapus state reservasi lain.

### Security notes

- Owner ID tidak diterima dari command/API/database dan tidak ditulis pada log atau respons.
- Command owner tidak memanggil `TicketService.create_or_get()`, tidak membuat notification outbox baru, dan tidak mengirim status kepada pelanggan.
- Phase F tidak menambahkan atau menjalankan migrasi database.

## 2026-07-21 - Telegram Owner Notification Outbox (V1.8 Phase E)

### Added

- Model dan migrasi manual `support_ticket_notifications` dengan FK tiket, channel/status/attempt CHECK, unique ticket-channel, serta index due-job dan lease.
- Transaksi atomik tiket baru dan satu job `telegram_owner`; reuse tiket dan pesan dalam lock tidak menambah job.
- Dispatcher sequential runner-only dengan PostgreSQL `FOR UPDATE SKIP LOCKED`, processing lease, bounded exponential backoff, recovery lease kedaluwarsa, dan error code allowlisted.
- Renderer plain text Indonesia dari field tiket allowlisted serta konfigurasi owner Telegram yang divalidasi hanya saat runner dimulai.
- Unit test offline dan PostgreSQL integration test opt-in memakai schema disposable serta `TEST_DATABASE_URL` saja.

### Security notes

- Outbox tidak menyimpan owner chat ID, customer UUID, raw Telegram ID, identity HMAC, session reference, pesan/transcript, rendered body, token, secret, URL, response body, atau exception text.
- FastAPI tidak memulai dispatcher dan tetap dapat berjalan saat konfigurasi owner Telegram hilang atau malformed.
- Telegram memiliki crash window eksternal setelah API menerima pesan sebelum status lokal menjadi `sent`; lease mengurangi risiko, tetapi bukan jaminan exactly-once delivery.

## 2026-07-20 - Persistent Support Ticket Hardening (V1.6 Phase C Fix 2)

### Added

- CHECK constraint PostgreSQL bernama stabil untuk priority dan status tiket.
- Partial unique index untuk membatasi satu tiket aktif (`open`/`in_progress`) per authenticated customer dan hash sesi percakapan.
- Lifecycle ticket `in_progress`, `resolved`, dan `closed` dengan update owner-filtered serta timestamp penyelesaian.
- Pemulihan automation lock dari tiket aktif PostgreSQL sebelum classifier, AI, atau workflow reservasi berjalan.
- PostgreSQL integration tests opt-in melalui `TEST_DATABASE_URL` dengan schema uji terisolasi dan penolakan database aplikasi normal.

### Changed

- Migrasi support ticket menjadi konvergen: tabel yang sudah ada tetap diperiksa dan index/constraint aman yang hilang ditambahkan.
- Tiket resolved/closed tidak lagi dipakai ulang; handoff berikutnya dapat membuat tiket aktif baru.
- Race concurrent tetap rollback lalu mengambil tiket aktif pemenang.

### Security notes

- Recovery menggunakan authenticated `owner_customer_id` dan SHA-256 customer-session reference; raw session, token, pesan, dan detail reservasi tidak dipulihkan atau dicatat.
- Migrasi support ticket tidak dijalankan selama implementasi ini dan tidak menyentuh tabel atau record reservasi.

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
- V1.6 Phase C: tiket support persisten untuk handoff, dengan nomor tiket dan referensi session yang di-hash.
# V1.7 Phase D - Telegram Customer Bot

- Menambahkan Telegram customer bot berbasis local long polling sebagai proses terpisah.
- Menambahkan identity Telegram persisten berbasis HMAC dan migrasi `telegram_identities` yang aditif.
- Menyatukan boundary chat terautentikasi untuk HTTP dan Telegram tanpa bearer token Telegram.
- Menambahkan private-chat-only handler, `/start`, `/help`, `/status`, serta pemecahan balasan plain text aman.
- Membatasi logger `httpx`, `httpcore`, dan Telegram serta menambahkan redaction credential dan PTB error handler aman.
- Memindahkan validasi Telegram-only ke runner, memperkuat handler failure, dan membuat `/status` deterministik tanpa AI.
- Memberi HMAC label purpose/version terpisah serta memperketat convergence migration dan test concurrency PostgreSQL.
