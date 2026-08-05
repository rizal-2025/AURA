# Fitur AURA

Dokumen ini menjelaskan kemampuan yang tersedia pada baseline AURA saat ini. Bagian terakhir membedakan fondasi yang sudah ada dari fitur yang belum selesai secara end-to-end.

## API aplikasi

| Endpoint | Fungsi |
| --- | --- |
| `GET /` | Mengembalikan nama aplikasi, versi, dan status berjalan. |
| `GET /health` | Health check sederhana dengan status layanan. |
| `POST /auth/guest` | Membuat pelanggan guest di server dan mengembalikan bearer access token beserta waktu kedaluwarsanya. |
| `POST /chat` | Menerima bearer token, `session_id` untuk memori percakapan, dan pesan pengguna. |
| `POST /reservation/` | Membuat reservasi dan mengembalikan opaque public reference. |
| `GET /reservation/` | Daftar reference-only milik bearer customer aktif. |
| `GET /reservation/{reference}` | Detail owner-scoped berdasarkan public reference. |
| `PATCH /reservation/{reference}` | Mengubah tepat satu business field berdasarkan public reference. |
| `POST /reservation/{reference}/cancel` | Membatalkan reservasi owner-scoped tanpa physical delete. |

FastAPI juga menyediakan spesifikasi OpenAPI dan antarmuka dokumentasi interaktif bawaan.

## Public reference contract - Phase C

- **Direct API reference-only:** seluruh response reservasi memakai `reference`
  kanonis dan mapper allowlist eksplisit; numeric database ID tidak masuk
  public DTO atau OpenAPI.
- **Ownership atomik:** list/detail/update/cancel selalu memperoleh owner dari
  authentication context dan memakai predicate owner + reference.
- **Demo DTO:** daftar demo memakai alias `reservationReference`, tetap
  session-scoped, dan satu stored reference tidak aman menggagalkan seluruh
  response secara generik.
- **Structured mutation:** response demo chat memuat nullable mutation dengan
  tepat `operation` dan `reservationReference`, dipetakan hanya dari typed
  `AgentTurnResult` tanpa parsing reply.
- **Durable replay:** metadata mutation tersimpan pada assistant completion row
  dan duplicate completed request membaca pasangan persisted yang sama tanpa
  mengulang core atau mutation bisnis.
- **Migration additive:** dua column nullable dan empat check constraint
  PostgreSQL menjaga enum, null-pair, assistant-only, dan canonical reference;
  row lama tetap valid sebagai null pair.
- **Batas tahap:** history DTO/content belum berubah dan belum dinyatakan
  public-safe. Phase D dan Phase E tetap memblokir Chat BFF.

## Serialisasi percakapan - V2.0 G1C

- **Keyed async lock:** satu authenticated customer-session hanya menjalankan
  satu operasi stateful pada satu waktu di dalam proses Python yang sama.
- **Cakupan penuh:** lock meliputi handoff recovery, memory, classifier/AI,
  Create/View/Update/Cancel, ticket/outbox, dan status tiket.
- **Konkurensi terisolasi:** customer atau session berbeda memakai lock berbeda
  dan tetap dapat diproses bersama.
- **Bounded wait:** setelah 15 detik HTTP menerima
  `409 CONVERSATION_BUSY`, sementara Telegram menerima respons busy generik.
- **Telegram bounded concurrency:** satu runner menerima maksimal delapan update
  bersamaan; owner command tidak masuk customer conversation lock.
- **Fail-safe cleanup:** exception, cancellation, atau timeout melepaskan lock
  dan membersihkan entry registry tanpa manual release API.
- **Batas proses:** satu Uvicorn worker dan satu polling process wajib digunakan.
  FastAPI dan Telegram pada proses terpisah tidak berbagi lock; distributed
  coordination belum tersedia.

## Transaction foundation - V2.0 G1D-A1

- **Ownership eksplisit:** ingress membuat/menutup session, service memiliki
  transaksi bisnis, dan repository hanya menjadi participant.
- **Satu commit:** mutation reservasi, lifecycle tiket owner, identity, serta
  fase database outbox memiliki satu controlled commit boundary.
- **Rollback aman:** kegagalan yang terbukti terjadi sebelum commit di-rollback;
  kegagalan pada batas commit dilaporkan sebagai outcome tidak pasti tanpa
  retry mutation otomatis.
- **DTO immutable:** caller tidak menerima ORM object yang memerlukan lazy load
  atau refresh setelah commit. DTO record persistence terpisah dari schema
  input sehingga record legacy tidak divalidasi ulang dengan batas Create
  terbaru, sementara setiap input baru tetap memakai validator ketat.
- **Ticket/outbox atomik:** tiket support baru dan satu pending notification
  distage dalam transaksi yang sama.
- **Network di luar transaksi:** AI/provider dan Telegram send tidak menahan
  transaksi database; send failure tidak membatalkan state yang sudah commit.
- **Adapter persistence aman:** error transaksi chat diteruskan ke respons
  channel-specific (`503` HTTP atau pesan generik Telegram), bukan diubah
  menjadi internal-error handoff.
- **Dispatcher terpisah:** klasifikasi failure Telegram hanya berlaku pada
  network send; failure database saat `mark_sent` mempertahankan row lease
  untuk rekonsiliasi.
- **UoW single-use:** satu instance hanya dapat memiliki satu lifecycle dan
  cleanup dependency mempertahankan exception aplikasi authoritatif.
- **Cancel reconciliation:** hasil mutation nol dapat diikuti owner-filtered
  read transaction baru setelah transaction mutation selesai.
- **ID reservasi nyata:** konfirmasi Create memakai primary key database yang
  benar, bukan UUID sesi buatan.
- **Batas tahap:** belum ada snapshot/recovery memory G1D-A2 dan belum ada
  idempotensi retry Create G1D-B. Tidak ada migration G1D-A1.

## Deep conversation snapshots - V2.0 G1D-A2.1

- **Snapshot per conversation:** satu state authenticated customer-session dapat
  dicapture sebagai struktur immutable tanpa menyalin global memory.
- **Deep isolation:** list/dictionary bertingkat tidak berbagi alias dengan
  live state, materialisasi caller, atau replacement state.
- **Atomic replacement:** rollback memory mengganti seluruh conversation state
  di dalam lock G1C dan tidak melakukan shallow merge.
- **Safe value boundary:** hanya nilai JSON-like yang aman diterima; Session,
  ORM object, UUID object, coroutine, lock, exception, dan arbitrary object
  ditolak dengan error stabil.
- **Bounded snapshot:** cycle, kedalaman di atas 16, lebih dari 256 item per
  container, atau lebih dari 2.048 node ditolak secara deterministik.
- **DB-first publication:** Create memublikasikan ID/completed, sedangkan Update
  dan Cancel membersihkan stage hanya setelah service commit berhasil kembali.
- **Exact pre-commit restore:** kegagalan persistence yang terkonfirmasi
  mengembalikan draft, selected field/ID, confirmation stage, serta nested state
  secara utuh.
- **Fail-closed uncertainty:** commit outcome unknown atau Session unusable
  membuat blocker reservasi process-local tanpa identifier/nilai privat dan
  mencegah mutation ulang otomatis.
- **Confirmed-commit memory guard:** kegagalan publication setelah commit yang
  telah dikonfirmasi memakai status `committed_memory_unavailable` dan emergency
  guard terpisah; hal ini tidak dinyatakan sebagai kegagalan database.
- **Non-mutating access while blocked:** View, greeting, pertanyaan
  informasional, dan explicit human escalation tetap tersedia; Create, Update,
  Cancel, serta continuation mutasinya tetap ditolak.
- **Post-commit durability:** failure format/send respons tidak mengembalikan
  memory sukses, mencoba rollback data committed, atau membuat tiket
  `internal_error`.
- **Batas tahap:** handoff recovery tetap G1D-A2.2/A2.3 dan Create idempotency
  tetap G1D-B. Blocker hilang saat process restart sehingga verifikasi manual
  diperlukan sebelum retry. Tidak ada migration A2.1.

## Persistent reservation workflow recovery - V2.0 G1D-A2.2

- **State minimal dan terisolasi:** unfinished Create, Update, Cancel, serta
  blocker mutation disimpan per authenticated customer dan hash sesi; raw
  referensi sesi dan state handoff tidak masuk payload.
- **Validasi fail-closed:** payload JSONB memakai schema version 1, empat bentuk
  canonical, enum stage/field, validator nilai reservasi yang sama, integer
  non-boolean, batas 4 KiB, dan penolakan kombinasi workflow yang konflik.
- **Recovery order:** restore dilakukan di dalam lock G1C sebelum handoff
  reconciliation dan sebelum classifier, AI, atau workflow berjalan.
- **Transaksi singkat:** restore read selesai sebelum agent await; publication
  memakai transaksi baru setelah turn.
- **Mutation safety:** pre-mutation marker, revision check, dan inactive
  tombstone mencegah konfirmasi stale serta stale writer menghidupkan workflow
  terminal setelah restart.
- **Corruption behavior:** row tidak otomatis dihapus; error aman menghentikan
  turn sehingga payload tetap tersedia untuk investigasi database terkontrol.
- **Migrasi:** `migrations/add_conversation_workflow_states.py` bersifat
  additive, idempoten, memvalidasi tabel existing, dan tidak menyentuh row
  `reservations`.
- **Batas tahap:** marker reconciliation bersifat konservatif; durable
  request-key idempotency dan lock lintas worker/instance tetap pekerjaan
  terpisah.

## Orkestrasi chat berbasis AI

- **Provider AI yang dapat dipilih:** AURA dapat memakai Ollama atau OpenAI melalui factory provider yang sama.
- **Klasifikasi intent:** pesan dapat diklasifikasikan sebagai `reservation`, `view_reservation`, `update_reservation`, `cancel_reservation`, `menu`, `promo`, `faq`, `complaint`, atau `general`, beserta nilai confidence dari classifier utama.
- **Planner dan workflow:** intent serta state percakapan diubah menjadi langkah kerja sebelum dijalankan oleh agent yang sesuai.
- **Fallback jawaban umum:** intent di luar jalur workflow reservasi diteruskan ke provider AI untuk menghasilkan respons.

## Indonesian NLU - V2.0 G1D-A2.3

- **Context-first:** jawaban untuk people/date/time, konfirmasi, selection ID,
  dan edit field ditafsirkan menurut stage workflow aktif sebelum routing intent.
- **Normalisasi aman:** varian informal umum dinormalisasi per token secara
  Unicode-safe dan punctuation-tolerant tanpa memodifikasi substring nama.
- **Intent natural:** greeting, Create, Update, Cancel, status/listing, dan
  explicit human handoff menerima frasa Indonesia yang lebih beragam.
- **Field natural:** angka/kata bilangan dan bentuk kelompok untuk people;
  tanggal kanonis/numerik/nama bulan/relatif/hari; waktu titik/titik dua,
  kata bilangan, periode hari, dan bentuk `setengah`.
- **One-shot:** semua field yang valid dan jelas diambil sekaligus; workflow
  hanya menanyakan field yang masih hilang.
- **Fail-closed ambiguity:** tanggal tanpa bulan/tahun dan jam tanpa periode
  meminta klarifikasi terarah.
- **Konfirmasi phrase-aware:** respons multi-kata seperti `ya lanjut`,
  `oke gas`, atau `sip lanjut` diterima hanya pada confirmation context;
  `tidak`, `jangan`, dan `batal` selalu mengambil precedence.
- **Structured AI fallback:** hanya objek JSON datar allowlisted yang diterima;
  enum, confidence, ukuran, dan setiap nilai field divalidasi deterministik.
  Output mentah/prose/provider error tidak menjadi workflow state.
- **Batas:** tidak ada operasi bisnis, migration, perubahan ownership,
  confirmation, recovery, persistence, atau transaction boundary baru.

## Reservation V1

- **Reservasi multi-turn:** sistem meminta data yang belum lengkap secara berurutan: nama, jumlah orang, tanggal, lalu waktu.
- **Ekstraksi informasi:** pesan seperti `atas nama Rizal`, `4 orang`, `besok`, dan `jam 7 malam` dapat diambil sebagai data reservasi.
- **Normalisasi tanggal dan waktu:** parser mendukung `hari ini`, `besok`, `lusa`, nama hari, serta ekspresi waktu Indonesia seperti `jam 7 malam` dan `setengah delapan malam`.
- **Memori sesi:** data reservasi dipertahankan per `session_id`, sehingga jawaban pada beberapa pesan dapat digabungkan, tidak tercampur dengan sesi lain, dan sesi dapat dihapus melalui memory manager.
- **Koreksi konteks:** perubahan sederhana dengan kata seperti `ganti` atau `ubah` dapat memperbarui data reservasi yang telah tersimpan di sesi.
- **Konfirmasi:** setelah semua field lengkap, AURA menampilkan ringkasan dan meminta jawaban **Ya** atau **Tidak**.
- **Penyimpanan:** jawaban positif membuat reservasi melalui service dan repository database; record baru memiliki status awal `pending` serta `owner_customer_id` dari bearer token tervalidasi.
- **Nomor reservasi:** setelah konfirmasi berhasil, AURA menyimpan dan
  menampilkan ID reservasi database yang benar pada state sesi.

## Reservation Management (READ) - V1.1

- **Lihat reservasi:** intent `view_reservation` menangani frasa seperti `lihat reservasi saya`, `reservasi saya`, `daftar reservasi`, dan `show my reservation`.
- **Daftar terbaru:** handler mengambil maksimal lima record dari tabel `reservations`, diurutkan berdasarkan ID menurun.
- **Format respons:** setiap record menampilkan ID, nama, jumlah orang, tanggal, jam, dan status.
- **Kepemilikan:** daftar hanya memuat maksimal lima record milik `owner_customer_id` dari bearer token aktif, tetap dengan urutan ID menurun.

## Reservation Management (UPDATE) - V1.2

- **Mulai update:** intent `update_reservation` menangani permintaan seperti `ubah reservasi saya` dan menampilkan maksimal lima reservasi terbaru.
- **Pilih record dan field:** pengguna memilih ID reservasi, lalu memilih `name`, `people`, `date`, atau `time`.
- **Pembaruan database:** nilai baru dinormalisasi untuk tanggal/waktu bila diperlukan, lalu disimpan melalui UPDATE PostgreSQL dan ditampilkan kembali sebagai ringkasan.
- **Validasi alur:** ID reservasi dan nama field yang tidak valid ditolak tanpa menjalankan update.
- **Kepemilikan:** pemilihan dan pembaruan ID dibatasi pada record milik `owner_customer_id` dari bearer aktif; ID pelanggan lain ditolak dengan respons aman. UPDATE repository memakai predicate ownership atomik.
- **Jumlah orang natural:** field `people` menerima tepat satu integer positif dari respons seperti `9 orang`, `menjadi 9 orang`, atau `ubah jadi 9`; angka negatif, desimal, nol, dan input ambigu ditolak sambil mempertahankan sesi update.

## Reservation Management (CANCEL) - V1.3

- **Mulai pembatalan:** intent `cancel_reservation` menangani `batalkan reservasi saya`, `cancel reservasi`, `saya ingin membatalkan reservasi`, dan `cancel my reservation`.
- **Pilih dan konfirmasi:** pengguna memilih ID dari maksimal lima reservasi terbaru, melihat ringkasan, lalu menjawab **Ya/Tidak** untuk mengonfirmasi pembatalan.
- **Status tanpa penghapusan:** jawaban **Ya** mengubah status record menjadi `cancelled`; jawaban **Tidak** mengakhiri operasi tanpa perubahan database.
- **Validasi:** ID yang tidak ditemukan serta reservasi dengan status `cancelled` ditolak; record yang telah dibatalkan tidak dapat dibatalkan kembali.
- **Kepemilikan:** daftar, pemilihan, dan pembatalan hanya berlaku pada record milik `owner_customer_id` dari bearer aktif; ID pelanggan lain tidak dapat dibatalkan. UPDATE status memakai predicate ownership atomik.

## Secure Customer Ownership - V1.5

- **Identitas terpisah:** `session_id` adalah kunci memori percakapan saja. UUID `owner_customer_id` selalu berasal dari token bearer yang divalidasi server; `X-Session-ID` tidak menentukan pemilik.
- **Autentikasi guest:** `POST /auth/guest` menerbitkan JWT HS256 dengan issuer, audience, expiry, customer UUID, dan token version. Token invalid, expired, inactive, atau version mismatch ditolak dengan 401.
- **Isolasi data:** fitur Create, Read, Update, dan Cancel selalu memakai `owner_customer_id` pelanggan terautentikasi. Pengguna tidak dapat melihat, mengubah, atau membatalkan reservasi pelanggan lain.
- **Record legacy:** kolom `customer_id` dan reservasi dengan `owner_customer_id = NULL` dipertahankan tanpa perubahan, tetapi tidak diekspos lewat fitur reservasi pelanggan.
- **Migrasi aman:** jalankan `migrations/add_secure_customer_identity.py` untuk fondasi V1.5. Skrip idempoten, tidak membuat ulang tabel `reservations`, tidak menghapus record, dan tidak melakukan backfill.

## Konfigurasi keamanan

- `APP_ENV` wajib persis salah satu dari `development`, `test`, `staging`, atau `production`; production tidak pernah diinferensikan.
- FastAPI, database/migrasi, dan runner Telegram memakai batas settings berbeda, sehingga secret Telegram tidak diperlukan oleh proses API.
- `AUTH_JWT_SECRET` wajib 32–512 karakter, bebas whitespace luar/control/placeholder/pengulangan trivial, dan tidak boleh dicatat atau dikomit.
- `AUTH_JWT_EXPIRE_MINUTES` wajib integer ketat 1–1440. Staging/production membutuhkan issuer/audience non-development.
- `AI_PROVIDER` hanya menerima `ollama` atau `openai`. Tidak ada fallback provider atau dummy OpenAI key.
- Runner Telegram menolak token/identity secret malformed, placeholder, control, dan whitespace luar; owner ID hanya wajib ketika command atau notification diaktifkan.
- Kegagalan startup menggunakan kode `CFG_*` tanpa raw nilai konfigurasi.
- Custom logger AURA hanya mencatat state transisi operasional; bearer token, secret, dan header `Authorization` tidak dicatat.

## Data dan memori

- **Persistensi reservasi:** model `Reservation`, repository, dan service memisahkan penyimpanan database dari layer API/agent, termasuk filter ownership berbasis `owner_customer_id` yang tervalidasi server.
- **Schema data:** Pydantic memvalidasi bentuk request chat dan data reservasi.
- **Input bounds V2.0 G1B:** `session_id`, pesan chat, nama, jumlah orang,
  tanggal, dan waktu memakai validator bersama pada HTTP, Telegram, service,
  Create, dan Update. Field tambahan serta coercion boolean/float/string untuk
  jumlah orang ditolak.
- **Unicode aman:** pesan tetap mendukung bahasa Indonesia, emoji, tanda baca,
  dan line break normal, tetapi NUL, control, bidi control, serta zero-width
  format character ditolak. Nama dinormalisasi NFC dengan alfabet tanda baca
  yang dibatasi.
- **HTTP body bounds:** request body maksimal 16.384 byte dengan early
  `Content-Length` rejection dan pembacaan bounded untuk body tanpa panjang.
- **Error validation aman:** respons `400`/`413`/`422` memakai kode stabil dan
  tidak menggemakan payload, credential, identifier, atau exception text.
- **Profil preferensi:** komponen long-term memory in-memory dapat menyimpan, mengambil, menggabungkan, memperbarui, dan menghapus preferensi nama, jumlah orang, waktu, serta meja favorit bila `user_id` tersedia pada state sesi.

## Komponen pendukung

- **Tool registry:** `ToolManager` dapat mendaftarkan dan menjalankan tool async melalui antarmuka yang seragam, serta mengembalikan error bila nama tool tidak terdaftar.
- **Tool database contoh:** `DatabaseTool` menerima parameter `query` dan mengembalikan respons terstruktur untuk demonstrasi integrasi tool.
- **Logging:** logger aplikasi menulis informasi proses dan error untuk membantu penelusuran alur.
- **Pengujian:** repository menyediakan test untuk classifier, planner, workflow, parser, memori, resolusi konteks, konfirmasi, routing, tool, dan provider AI.

## Fondasi yang belum merupakan fitur penuh

- Agent untuk cek reservasi, greeting, dan pertanyaan umum masih berupa placeholder; routing-nya ada, tetapi belum menjalankan logika bisnis penuh.
- Strategi planner untuk menu, promo, FAQ, dan keluhan sudah didefinisikan, tetapi belum memiliki handler chat khusus end-to-end.
- `DatabaseTool` belum menjalankan query ke database; saat ini hanya tool contoh.
- Hanya unfinished reservation workflow yang persisten. State percakapan umum,
  counter, dan long-term memory tetap berada di memori proses serta tidak
  dibagikan antar-instance.

Rencana untuk melengkapi fondasi tersebut dicatat di [ROADMAP.md](ROADMAP.md).

## Support Tickets

- Handoff membuat maksimal satu tiket aktif (`open` atau `in_progress`) per authenticated customer dan hash referensi percakapan.
- PostgreSQL menegakkan priority `low`, `medium`, `high`, atau `urgent` serta status `open`, `in_progress`, `resolved`, atau `closed`.
- Tiket `resolved` dan `closed` menjadi inactive, sehingga handoff baru pada customer-session yang sama dapat memperoleh nomor tiket baru.
- Pergantian status memakai predicate `ticket_id` dan `owner_customer_id`, memperbarui `updated_at`, serta menetapkan `resolved_at` untuk status terminal.
- Setelah restart, request chat memulihkan automation lock dari tiket aktif sebelum classifier, AI, Update, atau Cancel berjalan.
- Persistensi hanya menyimpan hash SHA-256 referensi sesi dan ringkasan operasional allowlisted; tidak menyimpan raw session, token, transcript, pesan pelanggan, atau detail reservasi.
- Tiket baru membuat satu job outbox notifikasi owner secara atomik; tiket aktif yang dipakai ulang tidak membuat job tambahan.
# Telegram Customer Bot (V1.7 Phase D)

- Percakapan reservasi melalui private chat Telegram memakai workflow AURA yang sama dengan chat HTTP.
- Identity Telegram dipetakan server-side ke Customer melalui HMAC; raw Telegram ID dan bearer token tidak disimpan.
- Handoff dan ticket persisten dipulihkan untuk customer dan sesi Telegram yang sama setelah restart.
- Perintah `/start`, `/help`, dan `/status` tersedia; command owner/admin belum tersedia.
- `/status` membaca tiket aktif berdasarkan Customer dan session HMAC tanpa memanggil AI, classifier, atau membuat tiket baru.
- Logging network dibatasi dan credential redaction melindungi token Bot API, bearer token, serta password DSN.
- Identity/session memakai domain HMAC versioned yang berbeda dan konfigurasi Telegram divalidasi hanya oleh runner.

# Telegram Owner Notification (V1.8 Phase E)

- Runner dapat mengirim notifikasi plain text aman untuk tiket baru ke private owner chat yang berasal eksklusif dari konfigurasi tervalidasi.
- Persistent transactional outbox mempertahankan job setelah restart, menghindari enqueue ganda, dan memulihkan lease `sending` yang kedaluwarsa.
- Klaim job memakai locking PostgreSQL, sementara retry memakai backoff dan maksimum attempt yang dibatasi konfigurasi.
- Isi notifikasi hanya memakai field support ticket yang diizinkan dan tidak menyimpan atau menampilkan identitas pelanggan, raw message, session reference, credential, atau detail reservasi.
- FastAPI tidak menjalankan dispatcher; Phase E tetap memakai single Telegram polling instance.

# Telegram Owner Ticket Management (V1.9 Phase F)

- Command `/tickets`, `/ticket`, `/take`, dan `/resolve` hanya tersedia bagi private owner chat yang dikonfigurasi dan tervalidasi runner.
- `/tickets` menampilkan maksimal 10 tiket aktif dengan urutan waktu pembuatan menaik; `/ticket` dapat melihat status aktif maupun terminal melalui DTO allowlisted.
- `/take` mengubah `open` menjadi `in_progress`; `/resolve` mengubah `open`/`in_progress` menjadi `resolved`. Pengulangan bersifat idempoten dan tiket terminal tidak dibuka kembali.
- Transisi memakai row lock PostgreSQL dan satu commit service; exception database selalu di-rollback tanpa dibocorkan ke Telegram.
- Penyelesaian tiket melepaskan automation lock customer-session terkait pada pesan berikutnya tanpa menghapus state reservasi lain.
- Command owner dan notification dispatcher dikonfigurasi independen. Command owner tidak membuat outbox notifikasi baru dan customer status notification belum tersedia.
- Tidak ada migrasi Phase F; owner command tetap mengikuti batas satu Telegram polling instance.
