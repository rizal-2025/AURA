# Bug Tracker

## Status saat ini

Saat ini tidak ada bug terbuka yang terdokumentasi atau dilaporkan pada repository AURA.

Pencarian pada source juga tidak menemukan penanda `TODO`, `FIXME`, `BUG`, atau `HACK` yang menunjuk ke issue terbuka.

## Audit V1.5 Phase 3A - fixed

Audit rilis sebelumnya menemukan beberapa risiko yang telah ditangani pada Phase 3A:

- Operasi reservasi secure sekarang gagal tertutup bila `owner_customer_id` tidak tersedia; nilai `None` tidak dapat berubah menjadi query `IS NULL` untuk record legacy.
- Pembuatan reservasi baru dengan `customer_id` legacy saja diblokir; skrip insert legacy dinonaktifkan.
- SQL echo nonaktif secara default, log state penuh dan raw AI response dihapus, dan log transisi memakai field yang diizinkan.
- `AUTH_JWT_EXPIRE_MINUTES` sekarang menerima hanya integer ketat 1--1440.

Record legacy tetap tersimpan dan tidak dimodifikasi.

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
