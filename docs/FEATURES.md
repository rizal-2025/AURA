# Fitur AURA

Dokumen ini menjelaskan kemampuan yang tersedia pada baseline AURA saat ini. Bagian terakhir membedakan fondasi yang sudah ada dari fitur yang belum selesai secara end-to-end.

## API aplikasi

| Endpoint | Fungsi |
| --- | --- |
| `GET /` | Mengembalikan nama aplikasi, versi, dan status berjalan. |
| `GET /health` | Health check sederhana dengan status layanan. |
| `POST /chat` | Menerima `session_id` dan pesan pengguna, lalu mengembalikan balasan AURA. |
| `POST /reservation/` | Membuat reservasi langsung dari data nama, jumlah orang, tanggal, dan waktu. |

FastAPI juga menyediakan spesifikasi OpenAPI dan antarmuka dokumentasi interaktif bawaan.

## Orkestrasi chat berbasis AI

- **Provider AI yang dapat dipilih:** AURA dapat memakai Ollama atau OpenAI melalui factory provider yang sama.
- **Klasifikasi intent:** pesan dapat diklasifikasikan sebagai `reservation`, `view_reservation`, `update_reservation`, `menu`, `promo`, `faq`, `complaint`, atau `general`, beserta nilai confidence dari classifier utama.
- **Planner dan workflow:** intent serta state percakapan diubah menjadi langkah kerja sebelum dijalankan oleh agent yang sesuai.
- **Fallback jawaban umum:** intent di luar jalur workflow reservasi diteruskan ke provider AI untuk menghasilkan respons.

## Reservation V1

- **Reservasi multi-turn:** sistem meminta data yang belum lengkap secara berurutan: nama, jumlah orang, tanggal, lalu waktu.
- **Ekstraksi informasi:** pesan seperti `atas nama Rizal`, `4 orang`, `besok`, dan `jam 7 malam` dapat diambil sebagai data reservasi.
- **Normalisasi tanggal dan waktu:** parser mendukung `hari ini`, `besok`, `lusa`, nama hari, serta ekspresi waktu Indonesia seperti `jam 7 malam` dan `setengah delapan malam`.
- **Memori sesi:** data reservasi dipertahankan per `session_id`, sehingga jawaban pada beberapa pesan dapat digabungkan, tidak tercampur dengan sesi lain, dan sesi dapat dihapus melalui memory manager.
- **Koreksi konteks:** perubahan sederhana dengan kata seperti `ganti` atau `ubah` dapat memperbarui data reservasi yang telah tersimpan di sesi.
- **Konfirmasi:** setelah semua field lengkap, AURA menampilkan ringkasan dan meminta jawaban **Ya** atau **Tidak**.
- **Penyimpanan:** jawaban positif membuat reservasi melalui service dan repository database; record baru memiliki status awal `pending`.
- **Nomor reservasi sesi:** setelah konfirmasi berhasil, AURA menyimpan UUID sebagai nomor reservasi pada state sesi dan menampilkannya pada respons.

## Reservation Management (READ) - V1.1

- **Lihat reservasi:** intent `view_reservation` menangani frasa seperti `lihat reservasi saya`, `reservasi saya`, `daftar reservasi`, dan `show my reservation`.
- **Daftar terbaru:** handler mengambil maksimal lima record dari tabel `reservations`, diurutkan berdasarkan ID menurun.
- **Format respons:** setiap record menampilkan ID, nama, jumlah orang, tanggal, jam, dan status.
- **Batasan kepemilikan:** schema saat ini belum memiliki `user_id` atau `session_id`, sehingga daftar V1.1 berisi lima reservasi terbaru secara global, bukan hasil filter per pengguna.

## Reservation Management (UPDATE) - V1.2

- **Mulai update:** intent `update_reservation` menangani permintaan seperti `ubah reservasi saya` dan menampilkan maksimal lima reservasi terbaru.
- **Pilih record dan field:** pengguna memilih ID reservasi, lalu memilih `name`, `people`, `date`, atau `time`.
- **Pembaruan database:** nilai baru dinormalisasi untuk tanggal/waktu bila diperlukan, lalu disimpan melalui UPDATE PostgreSQL dan ditampilkan kembali sebagai ringkasan.
- **Validasi alur:** ID reservasi dan nama field yang tidak valid ditolak tanpa menjalankan update.
- **Batasan kepemilikan:** tanpa identitas pengguna pada schema, pilihan ID berasal dari daftar reservasi terbaru secara global.

## Data dan memori

- **Persistensi reservasi:** model `Reservation`, repository, dan service memisahkan penyimpanan database dari layer API/agent.
- **Schema data:** Pydantic memvalidasi bentuk request chat dan data reservasi.
- **Profil preferensi:** komponen long-term memory in-memory dapat menyimpan, mengambil, menggabungkan, memperbarui, dan menghapus preferensi nama, jumlah orang, waktu, serta meja favorit bila `user_id` tersedia pada state sesi.

## Komponen pendukung

- **Tool registry:** `ToolManager` dapat mendaftarkan dan menjalankan tool async melalui antarmuka yang seragam, serta mengembalikan error bila nama tool tidak terdaftar.
- **Tool database contoh:** `DatabaseTool` menerima parameter `query` dan mengembalikan respons terstruktur untuk demonstrasi integrasi tool.
- **Logging:** logger aplikasi menulis informasi proses dan error untuk membantu penelusuran alur.
- **Pengujian:** repository menyediakan test untuk classifier, planner, workflow, parser, memori, resolusi konteks, konfirmasi, routing, tool, dan provider AI.

## Fondasi yang belum merupakan fitur penuh

- Agent untuk cek reservasi, pembatalan reservasi, greeting, dan pertanyaan umum masih berupa placeholder; routing-nya ada, tetapi belum menjalankan logika bisnis penuh.
- Strategi planner untuk menu, promo, FAQ, dan keluhan sudah didefinisikan, tetapi belum memiliki handler chat khusus end-to-end.
- `DatabaseTool` belum menjalankan query ke database; saat ini hanya tool contoh.
- Sesi dan long-term memory masih berada di memori proses, sehingga belum persisten setelah aplikasi restart atau dibagikan antar-instance.

Rencana untuk melengkapi fondasi tersebut dicatat di [ROADMAP.md](ROADMAP.md).
