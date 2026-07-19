# Changelog

Semua perubahan penting pada AURA dicatat di dokumen ini. Repository belum memiliki tag rilis, sehingga entri berikut mengikuti riwayat commit proyek.

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
