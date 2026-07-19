# Roadmap AURA

Dokumen ini merangkum status pengembangan berdasarkan baseline kode AURA saat ini. Item pada **Core** sudah tersedia; item pada **Next Version** dan **Future** adalah arah pengembangan, bukan fitur yang telah dirilis.

## Core - selesai

### Platform API

- Aplikasi FastAPI dengan endpoint root, health check, chat, dan pembuatan reservasi langsung.
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

### Fondasi pendukung

- Memori profil pengguna jangka panjang berbasis in-memory.
- Registry tool async dengan tool database contoh.
- Logging aplikasi, parser tanggal/waktu Indonesia, dan rangkaian test untuk komponen utama.

## Next Version

- Menyelesaikan siklus reservasi setelah dibuat: cek status per pengguna, jadwal ulang lanjutan, dan batalkan reservasi.
- Mengganti agent placeholder untuk cek reservasi, pembatalan, greeting, dan pertanyaan umum dengan perilaku yang benar-benar fungsional.
- Menyatukan daftar intent pada classifier, planner, dan workflow; melengkapi handler khusus untuk menu, promo, FAQ, dan keluhan.
- Menambahkan validasi bisnis untuk jumlah orang, format/tanggal/waktu reservasi, serta penanganan respons AI yang tidak valid.
- Membuat sesi dan profil preferensi persisten serta mendukung `user_id` melalui API agar personalisasi dapat dipakai end-to-end.
- Memperkuat reliabilitas provider AI dengan timeout, retry, dan error handling yang jelas.
- Menambah pengujian end-to-end yang terisolasi, termasuk database uji dan tanggal/waktu yang deterministik.

## Future

- Validasi ketersediaan meja, kapasitas, jam operasional, dan pencegahan bentrok jadwal.
- Notifikasi reservasi melalui kanal yang relevan, misalnya email atau WhatsApp.
- Autentikasi, otorisasi, dan dashboard operasional untuk staf restoran.
- Observabilitas produksi: metrik, tracing, audit log, rate limiting, dan alerting.
- Penyimpanan memori yang dapat dibagi antar-instance serta strategi deployment yang siap skala.
