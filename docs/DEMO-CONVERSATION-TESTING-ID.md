# Menjalankan qualification offline

Jalankan hanya pada worktree terisolasi. Gunakan interpreter terpasang secara
read-only; semua import aplikasi harus berasal dari cwd worktree, bukan master.
Tidak perlu install dependency, edit .env, atau konfigurasi environment global.

Wrapper baru memakai **fungsi child-process environment runner resmi**, bukan
menyalin credential. `test.pgpass` hanya melalui libpq dan ACL helper. Targetnya
tetap loopback `aura_test` / `aura_test_runner`. Runner resmi tidak dimodifikasi.

Contoh yang dijalankan dari worktree run ini (ganti lokasi report untuk run baru;
file evidence tidak boleh sudah ada):

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tools\Run-DemoHardeningQualification.ps1 -PythonPath C:\Users\RIZAL-SKYLINK\Documents\AURA\.venv\Scripts\python.exe -Suite broad -Seed 20260905 -Report C:\Users\RIZAL-SKYLINK\Documents\AURA-AUTOPILOT\reports\demo-hardening-20260905-163703\sweep1-broad.json
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tools\Run-DemoHardeningQualification.ps1 -PythonPath C:\Users\RIZAL-SKYLINK\Documents\AURA\.venv\Scripts\python.exe -Suite postgresql -Seed 20260905 -Report C:\Users\RIZAL-SKYLINK\Documents\AURA-AUTOPILOT\reports\demo-hardening-20260905-163703\sweep1-postgresql.json
```

Ulangi berurutan pada tree final yang sama dengan seed 20260906 dan nama
`sweep2-*.json`. Setelah commit, jalankan `-Suite critical` dan PostgreSQL lagi.

Fixture event-sink subprocess kini memakai `sys.executable` + `-B`, bukan
asumsi `.venv` berada di cwd worktree. APP_ENV/dotenv-off diteruskan eksplisit;
PYTHONPATH dan cwd tetap worktree. Ini perbaikan portabilitas test saja, setelah
empat WinError2 direproduksi pada worktree tanpa `.venv` lokal.

## Coverage dan batas hitungan

- 68 curated dialogue contracts baru: 64 field/value/locale recovery, Jessica,
  pembanding clock 10:00, invalid-calendar partial recovery, inline correction.
- 556 generated parser cases unik: 412 time surface forms setelah deduplication,
  144 kalender termasuk leap years. Ekspektasi dari tabel semantik/stdlib date.
- 100 distinct final calendar oracles per seeded sweep, fresh agent + workflow
  restore/publish per turn. Branch reset dan interposed small talk memakai seed.
  UUID tidak dihitung sebagai skenario unik. Dua seed bukan otomatis 200 unik.
- PostgreSQL: public update, persisted dialogue campaign, owner-scoped reset;
  tambah 10 pengulangan masing-masing dari 5 row-lock dan 3 chat/reset patterns.
  Repetisi 80 itu tidak dihitung sebagai 80 skenario unik baru.
- Scope legacy broad mencakup general ID/EN, locale/history/provider spy, create
  confirmation, read/cancel ownership, malformed/timeout/429/5xx mapping,
  reset/session, parser dan transaction rollback. Lihat test IDs, bukan klaim
  semua skenario melewati HTTP frontend→backend dalam satu test.

Database/dialogue campaign memakai agent, parser, service, workflow, repository
asli. Stage awal beberapa matrix di-seed; persistence dibaca ulang setiap turn.
SQLite membuktikan state contract, **bukan** row locking; locking dibuktikan oleh
backend PID/blocking PID pada PostgreSQL. Tidak ada real provider call.

Broad runner tidak menjalankan file bernama windows atau modul cleanup job,
cleanup task roundtrip, PostgreSQL operational runner, dan UAT preflight host.
Satu protected-ACL test Windows bisa skip jika bukan Administrator; jangan
menyamakan permission denied dengan fitur/task yang hilang. PostgreSQL required
skip membuat command gagal.

## Browser dan frontend

Frontend diaudit pada detached worktree terpisah, dependency lama tanpa install.
46 BFF/browser-client tests menggunakan synthetic fetch fixtures. Browser baru
tidak memakai cookie pengguna, server hanya loopback port 3187 milik run; tidak
reuse server Production. Locale/history/mobile specs lulus dengan API fixture.
Ini berbeda dari end-to-end provider live atau validation Production.

## Evidence

Result JSON di luar Git memuat count, IDs kegagalan, alasan skip, seed, source
root dan runtime. Issue ledger/checkpoint/report menyimpan temuan dan keputusan,
bukan secret/log Production. Kegagalan environment atau counterexample tidak
dihapus dengan retry-until-green. Setelah patch baru, ulang dua sweep final.

Status live yang sah untuk kampanye ini:
`LIVE_MODEL_QUALIFICATION=NOT_RUN_NO_APPROVAL`, `PRODUCTION_VALIDATION=NOT_RUN`.
