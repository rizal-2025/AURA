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

- 114 curated dialogue contracts: 68 awal, 32 corrective, dan 14 year/temporal
  contracts dari kasus Victor. Yang awal
  mencakup 64 field/value/locale recovery, Jessica,
  pembanding clock 10:00, invalid-calendar partial recovery, inline correction.
  Corrective: 12 invalid-date recovery, 5 combined create, 10 English time updates,
  5 date-only combined input updates dengan non-target time preservation.
  Tambahan Victor: 1 alur asli dengan clock 5 September 2026 22:24 Jakarta,
  2 combined-date/time locale cases, 4 confirmation-year-edit cases,
  1 date-with-collected-time case, dan 6 temporal confirmation-edit cases.
  Semuanya memakai shared SQLite/PostgreSQL contracts dan restore durable tiap turn.
- 556 generated parser cases unik: 412 time surface forms setelah deduplication,
  144 kalender termasuk leap years. Ekspektasi dari tabel semantik/stdlib date.
- 8612 unique property-style parser inputs di test_demo_blocker_fix:
  day 1..31, semua month aliases ID/EN, valid/invalid years, numeric separators,
  ambiguity conjunctions, dan structurally attached AM/PM dengan grammatical am.
  Gabungan dengan 556 awal = 9164 input unik (4 overlap), atau 9168 checks
  per sweep. Rerun tidak dihitung sebagai input baru.
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

## Corrective full PostgreSQL gate (setelah pre-merge blockers)

Selection lama `-Suite postgresql` sengaja tetap tersedia untuk targeted checks.
Gunakan `-Suite postgresql-full` untuk final gate: 17 modul integration PostgreSQL
yang direview, termasuk 9 modul extended yang gagal pada review sebelumnya.
Full mode juga memuat rate-limit/cleanup **service DB test**, bukan Windows
Scheduler/host-operation tests. Existing migration helpers hanya menyiapkan
namespace fixture unik di aura_test, tidak menerapkan migration aplikasi.

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tools\Run-DemoHardeningQualification.ps1 -PythonPath C:\Users\RIZAL-SKYLINK\Documents\AURA\.venv\Scripts\python.exe -Suite postgresql-full -Seed 2026090521 -Report C:\Users\RIZAL-SKYLINK\Documents\AURA-AUTOPILOT\reports\blocker-fix-b6f56ee-20260905\sweep1-full.json
```

Pilih NEW report path untuk tiap run. `discovered` adalah jumlah sebelum 80 repeat;
`run` mencakup repeat; `passed` dihitung lewat successful test-method callbacks,
bukan run-minus-error-events (satu method dapat punya beberapa subtest errors).
Skip tetap ditampilkan terpisah dan setiap required PostgreSQL skip membuat fail.

Historical fixtures pada G1D transactions, memory publication, restart recovery,
demo chat, dan public reference memakai test-only install_reservation_clock.
Fallback clock dibekukan pada 1 Agustus 2026 10:00 Jakarta (demo chat memakai clock
session test sendiri). Caller dengan clock eksplisit tetap authoritative.
Patch clock di-scope per test dan dibersihkan otomatis, termasuk setup failure;
validasi/service/domain tidak diganti. test_postgresql_fixture_clock membuktikan
past date/time masih ditolak, same-minute diterima, explicit clock dihormati,
dan bindings dikembalikan setelah cleanup.
