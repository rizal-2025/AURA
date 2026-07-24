# Roadmap AURA

Dokumen ini merangkum status pengembangan berdasarkan baseline kode AURA saat ini. Item pada **Core** sudah tersedia; item pada **Next Version** dan **Future** adalah arah pengembangan, bukan fitur yang telah dirilis.

## Core - selesai

### Platform API

- Aplikasi FastAPI dengan endpoint root, health check, guest authentication, chat, dan pembuatan reservasi langsung.
- Konfigurasi aplikasi berbasis environment serta provider AI yang dapat dipilih: Ollama atau OpenAI.
- Schema Pydantic untuk input chat dan data reservasi.

### Reservation V1

- Alur reservasi percakapan multi-turn untuk mengumpulkan nama, jumlah orang, tanggal, dan waktu.
- Klasifikasi intent, perencanaan langkah, dan routing workflow untuk alur reservasi.
- Ekstraksi entitas reservasi dari pesan berbahasa Indonesia serta normalisasi tanggal dan waktu.
- Penyimpanan state per `session_id`, koreksi data sederhana dalam percakapan, dan konfirmasi **Ya/Tidak** sebelum penyimpanan.
- Persistensi reservasi ke database melalui SQLAlchemy, service, dan repository.
- Endpoint REST untuk membuat reservasi langsung tanpa percakapan.
- Reservation Management (READ): intent `view_reservation` untuk menampilkan maksimal lima reservasi terbaru.
- Reservation Management (UPDATE): intent `update_reservation` untuk mengubah nama, jumlah orang, tanggal, atau jam pada reservasi tersimpan.
- Reservation Management (CANCEL): intent `cancel_reservation` untuk memilih salah satu dari lima reservasi terbaru, mengonfirmasi pembatalan, lalu mengubah status menjadi `cancelled` tanpa menghapus record.
- Secure Customer Identity (V1.5): server menerbitkan guest bearer token; `session_id` hanya untuk memori percakapan, sedangkan Create, Read, Update, dan Cancel memakai `owner_customer_id` dari pelanggan terautentikasi.
- Validasi konfigurasi JWT aman: secret minimal 32 karakter dan expiry positif, disertai regression test token, ownership, parser nilai natural, dan logging tanpa secret/token.
- Kolom `customer_id` V1.4 serta record legacy tetap dipertahankan; record dengan `owner_customer_id = NULL` tidak diekspos.
- Persistent Support Tickets (V1.6 Phase C): constraint database, satu tiket aktif per customer-session, lifecycle resolved/closed, race recovery, dan pemulihan automation lock setelah restart.
- Telegram Customer Bot (V1.7 Phase D): private long polling, persistent HMAC identity, shared authenticated chat workflow, dan `/start`, `/help`, `/status`.
- Telegram Owner Notification (V1.8 Phase E): transactional outbox tiket baru, runner-only sequential dispatcher, safe renderer, lease recovery, dan bounded retry.
- Telegram Owner Ticket Management (V1.9 Phase F): command owner private-chat, read model allowlisted, locked/idempotent take-resolve, serta pelepasan automation lock melalui revalidasi tiket persisten.
- Production Security Foundation (V2.0 G1A): `APP_ENV` wajib, settings per proses, validasi JWT/Telegram/AI fail-closed, kode error konfigurasi aman, tanpa fallback Ollama atau dummy OpenAI key.
- Input and HTTP Body Bounds (V2.0 G1B): validator bersama untuk chat,
  Telegram, Create/Update reservasi, schema strict tanpa extra field, respons
  validasi tanpa raw payload, serta batas request body 16 KiB.

### Fondasi pendukung

- Memori profil pengguna jangka panjang berbasis in-memory.
- Registry tool async dengan tool database contoh.
- Logging aplikasi, parser tanggal/waktu Indonesia, dan rangkaian test untuk komponen utama.

## Next Version

- V2.0 G1C: serialisasi async per authenticated customer-session.
- V2.0 G1D: transaction ownership reservasi, atomic status policy, dan handoff recovery sementara yang bounded.
- Menyelesaikan siklus reservasi setelah dibuat: cek status per pengguna dan jadwal ulang lanjutan.
- Mengganti agent placeholder untuk cek reservasi, greeting, dan pertanyaan umum dengan perilaku yang benar-benar fungsional.
- Menyatukan daftar intent pada classifier, planner, dan workflow; melengkapi handler khusus untuk menu, promo, FAQ, dan keluhan.
- Menambahkan validasi bisnis untuk jumlah orang, format/tanggal/waktu reservasi, serta penanganan respons AI yang tidak valid.
- Menambahkan refresh-token lifecycle atau autentikasi akun setelah kebutuhan produk dan kebijakan keamanan ditetapkan.
- Memperkuat reliabilitas provider AI dengan timeout, retry, dan error handling yang jelas.
- Menambah pengujian end-to-end yang terisolasi, termasuk database uji dan tanggal/waktu yang deterministik.
- Menjalankan suite PostgreSQL opt-in secara rutin pada CI dengan database disposable melalui `TEST_DATABASE_URL`.
- Uji migration/concurrency Phase E secara rutin pada PostgreSQL disposable di CI.
- Menambahkan customer status notification terpisah bila kebutuhan produk dan privasinya telah ditetapkan.

## Future

- Validasi ketersediaan meja, kapasitas, jam operasional, dan pencegahan bentrok jadwal.
- Notifikasi reservasi melalui kanal yang relevan, misalnya email atau WhatsApp.
- Registrasi, pemulihan akun, dan autentikasi pelanggan permanen dengan kebijakan verifikasi yang sesuai.
- Observabilitas produksi: metrik, tracing, audit log, rate limiting, dan alerting.
- Penyimpanan memori yang dapat dibagi antar-instance serta strategi deployment yang siap skala.
- Deployment Telegram multi-instance/webhook dengan koordinasi dispatcher dan observabilitas delivery.
# V1.7

- Phase D selesai secara implementasi: Telegram customer bot dengan identity persisten dan local long polling.
- Security/reliability hardening Phase D: redaction token, runner-only validation, deterministic status, handler fail-safe, dan migration/concurrency tests.
- Phase E selesai secara implementasi; aktivasi membutuhkan migrasi manual dan UAT runner pada database/Telegram test yang terisolasi.
