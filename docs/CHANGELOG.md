# Changelog

Semua perubahan penting pada AURA dicatat di dokumen ini. Repository belum memiliki tag rilis, sehingga entri berikut mengikuti riwayat commit proyek.

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

- Tabel saat ini belum menyimpan identitas pemilik reservasi, sehingga V1.1 menampilkan lima reservasi terbaru secara global.

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
