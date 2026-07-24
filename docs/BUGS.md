# Bug Tracker

## Status saat ini

Temuan audit Phase D tentang token logging, validasi konfigurasi runner, handler failure, status tiket, dan partial-schema migration telah diperbaiki. Phase E menambahkan transactional outbox dan masih memerlukan verifikasi PostgreSQL opt-in serta UAT aman sebelum aktivasi pada lingkungan normal.

Audit Phase F menemukan lock handoff memory dapat tetap aktif setelah tiket diselesaikan di PostgreSQL pada proses yang masih berjalan. Masalah tersebut telah diperbaiki: pesan berikutnya merekonsiliasi tiket aktif berdasarkan authenticated customer-session, melepas hanya state handoff bila tiket sudah terminal, dan mempertahankan state workflow lain. Lock akibat kegagalan awal persistensi tiket tetap fail-safe.

Pencarian pada source juga tidak menemukan penanda `TODO`, `FIXME`, `BUG`, atau `HACK` yang menunjuk ke issue terbuka.

## Audit V2.0 Phase G1A - fixed

- `APP_ENV` kini wajib dan exact; production tidak dapat diinferensikan dari default.
- Konfigurasi FastAPI tidak lagi memuat secret Telegram dan handler tidak memiliki fallback identity secret global.
- Secret JWT/Telegram menolak placeholder, whitespace luar, control character, panjang tidak aman, dan pengulangan trivial.
- Provider AI tidak lagi fallback diam-diam ke Ollama; OpenAI tidak lagi menggunakan dummy key.
- URL Ollama remote plaintext ditolak pada staging/production, sementara loopback development tetap didukung.
- Kegagalan konfigurasi hanya mengeluarkan kode `CFG_*` tanpa raw secret, token, URL, ID, atau environment value.
- Temuan adversarial lanjutan tentang mutation bypass, secret-bearing `repr`, bootstrap environment test, Unicode format/control, placeholder embedded, repetisi unit panjang, dan urutan konstruksi startup telah diperbaiki.
- Full test discovery kini dibedakan dari test top-level dan selalu menemukan suite `tests/integration`; PostgreSQL tetap opt-in melalui `TEST_DATABASE_URL`.

G1A tidak mengubah schema/database. Serialisasi percakapan dan hardening
transaksi tetap dicatat sebagai G1C/G1D, bukan bug yang diklaim selesai.

## Audit V2.0 Phase G1B - fixed after adversarial review

- Input chat dan reservasi memiliki batas panjang/karakter yang sama pada HTTP,
  Telegram, shared service, Create, dan Update.
- Coercion `people` dari boolean, float, atau string ditolak; rentang yang
  diterima hanya integer 1–20.
- Control, bidi control, zero-width format character, field JSON ekstra, serta
  tanggal/waktu direct API yang tidak kanonis ditolak.
- Request body dibatasi 16.384 byte tanpa unbounded buffering. Framing panjang
  malformed, konflik, atau tidak cocok menghasilkan respons generik.
- Representasi duplicate `Content-Length` dibandingkan sebelum konversi
  integer, sehingga `3`/`03` dan `3,003` ditolak sementara token mentah identik
  tetap diterima.
- Service Create selalu merevalidasi mapping field baru; model termutasi atau
  hasil `model_construct()` tidak dapat mengirim nilai nonkanonis ke repository.
- Koma sebelum clause reservasi dipisahkan oleh extractor tanpa memperluas
  allowlist nama. Nama punctuation-only/mark-only ditolak.
- Frame body dibatasi 1.024; disconnect sebelum body lengkap tidak menjalankan
  endpoint dan tidak memantulkan data.
- Error `422` tidak lagi memuat raw payload/input pada respons.

G1B tidak mengubah schema/database. Kebijakan tanggal lampau dan availability
belum ditegakkan; validator tanggal saat ini sengaja hanya memeriksa tanggal
kanonis yang nyata agar tidak bergantung pada timezone host. Parsing tanggal
relatif memakai UTC+7 sebagai timezone bisnis AURA yang disengaja.

## Audit V2.0 Phase G1C - fixed with documented process boundary

- Pesan untuk authenticated customer-session yang sama kini diserialisasi dari
  handoff recovery sampai workflow, repository/ticket/outbox, dan mutasi memory.
- Lock timeout setelah 15 detik tidak fallback ke pemrosesan concurrent; HTTP
  mengembalikan `409 CONVERSATION_BUSY` dan Telegram memakai respons generik.
- Holder exception/cancellation, waiter cancellation, dan timeout melepaskan
  reference serta membersihkan registry. Same-task reentrancy ditolak.
- Status tiket memakai lock manager yang sama dengan chat state-changing.
- Runner Telegram tidak lagi melakukan global serialization; maksimal delapan
  update dapat berjalan bersama dan keyed lock mengisolasi percakapan yang sama.

Keterbatasan yang masih terbuka dan disengaja: lock G1C hanya in-process.
Deployment wajib satu Uvicorn worker dan satu polling process. FastAPI dan
Telegram dalam proses berbeda tidak berbagi lock, sehingga satu percakapan
logis harus konsisten melalui satu ingress process. Distributed coordination
serta transaction/memory rollback tetap pekerjaan lanjutan, bukan klaim G1C.
Tidak ada schema atau migration G1C.

Perintah tiket owner juga tidak mengambil customer conversation lock. Row lock
database tetap melindungi transisi status tiket, tetapi resolve owner yang
berpacu dengan customer handoff restoration dapat membuat customer menerima
satu respons stale bahwa bantuan petugas masih ditunggu. Tiket terminal tidak
dibuka kembali, dan pesan customer berikutnya merekonsiliasi state tersebut.
Koordinasi lifecycle dan transaction secara penuh tetap pekerjaan G1D; race ini
tidak dinyatakan selesai oleh G1C dan tidak membutuhkan perubahan schema.

## Audit V1.5 Phase 3A - fixed

Audit rilis sebelumnya menemukan beberapa risiko yang telah ditangani pada Phase 3A:

- Operasi reservasi secure sekarang gagal tertutup bila `owner_customer_id` tidak tersedia; nilai `None` tidak dapat berubah menjadi query `IS NULL` untuk record legacy.
- Pembuatan reservasi baru dengan `customer_id` legacy saja diblokir; skrip insert legacy dinonaktifkan.
- SQL echo nonaktif secara default, log state penuh dan raw AI response dihapus, dan log transisi memakai field yang diizinkan.
- `AUTH_JWT_EXPIRE_MINUTES` sekarang menerima hanya integer ketat 1--1440.

Record legacy tetap tersimpan dan tidak dimodifikasi.

## Audit V1.6 Phase C - fixed

- Transaction ticket gagal sekarang selalu rollback dan session SQLAlchemy tetap dapat digunakan.
- Race concurrent memakai partial unique index tiket aktif, rollback, lalu mengambil nomor tiket pemenang.
- Nilai priority/status divalidasi pada aplikasi dan ditegakkan oleh CHECK constraint PostgreSQL.
- Constraint lama yang membatasi satu tiket sepanjang masa diganti secara aman dengan uniqueness untuk status aktif saja.
- Automation lock dapat dipulihkan dari tiket aktif PostgreSQL setelah memory proses hilang.
- Recovery dan persistensi tetap memakai ringkasan allowlisted serta hash customer-session tanpa raw message atau raw session ID.

PostgreSQL integration tests memerlukan `TEST_DATABASE_URL` khusus. Tanpa konfigurasi tersebut test ditandai skipped, bukan dianggap lulus terhadap PostgreSQL.

## Catatan ruang lingkup

Beberapa kemampuan masih berupa fondasi atau placeholder, misalnya cek reservasi, agent umum, dan `DatabaseTool`. Identitas pelanggan saat ini masih guest-only: belum ada refresh token, registrasi akun, maupun pemulihan akun. Hal tersebut dicatat sebagai pekerjaan pengembangan dan risiko rilis di [ROADMAP.md](ROADMAP.md), bukan sebagai bug terbuka.

## Format pelaporan berikutnya

Jika bug ditemukan, tambahkan entri dengan format berikut:

```markdown
## [OPEN] Judul singkat

- Dampak:
- Langkah reproduksi:
- Hasil yang diharapkan:
- Hasil aktual:
- Status:
```

- Keterbatasan saat ini: customer status notification, webhook deployment, dan multi-runner belum tersedia. Crash tepat setelah Telegram menerima pesan tetapi sebelum status `sent` tersimpan dapat menyebabkan satu retry duplikat setelah lease; ini adalah batas eksternal yang didokumentasikan, bukan klaim exactly-once.
## V1.7 Phase D — security fixes

- Logger network pihak ketiga dibatasi dan output memiliki credential redaction.
- Telegram-only configuration divalidasi runner; FastAPI tetap independen.
- Handler missing-object/send-failure, deterministic `/status`, HMAC domain separation, dan migration partial-schema telah diperkuat.
- PostgreSQL concurrency/migration tests tetap wajib dijalankan melalui dedicated `TEST_DATABASE_URL` sebelum migration normal.
