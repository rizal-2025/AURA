# Manual singkat demo publik AURA

Jalankan semua perintah dari folder utama repositori AURA. Demo sengaja tidak
aktif otomatis setelah Windows restart. Jangan bagikan nilai rahasia atau alamat
publik melalui chat.

## A. Menyalakan demo

Pastikan layanan Windows PostgreSQL dan Tailscale aktif, lalu jalankan:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
  .\deploy\windows\Start-AuraPublicDemo.ps1 -Profile production
```

Tunggu penanda `AURA_PUBLIC_DEMO_READY profile=production`. Jika demo memang
sudah sehat, hasilnya `AURA_PUBLIC_DEMO_ALREADY_READY profile=production`.
Menjalankan perintah dua kali tidak membuat proses atau sesi baru.

## B. Mengecek demo aktif

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
  .\deploy\windows\Get-AuraPublicDemoStatus.ps1 -Profile production
```

- `state=ready`: demo siap.
- `state=offline`: demo tidak terbuka ke publik; ini normal setelah restart atau
  setelah Stop.
- `state=degraded`: lihat `reason_codes`, jangan mematikan proses secara acak.

Perintah status hanya membaca kondisi. Perintah ini tidak membuat sesi dan tidak
mengubah database.

## C. Mematikan demo

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
  .\deploy\windows\Stop-AuraPublicDemo.ps1 -Profile production
```

Tunggu `AURA_PUBLIC_DEMO_STOPPED profile=production`. Perintah menghentikan
Funnel lebih dahulu, lalu AURA. PostgreSQL dan layanan Tailscale tetap berjalan.
Perintah aman dijalankan dua kali.

## D. Backup

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
  .\deploy\windows\Invoke-AuraProductionBackup.ps1 -Profile production
```

Hasil yang benar menunjukkan `archive_valid=yes` dan `acl_protected=yes`.
Klasifikasi umur backup: `fresh` sampai 24 jam, `warning` sampai 48 jam,
`stale` lebih dari 48 jam, dan `missing` jika belum ada. Backup tidak melakukan
restore.

## E. Kalau error

1. Jalankan status.
2. Jika `offline`, jalankan Start satu kali.
3. Jika PostgreSQL tidak berjalan, aktifkan layanan PostgreSQL Windows, lalu cek
   status lagi.
4. Jika ada kode ACL, listener, firewall, atau ownership, berhenti dan hubungi
   pemilik deployment. Jangan kill `python.exe` atau `tailscale.exe`.
5. Jika Funnel stale, jangan menjalankan reset. Metadata stale yang aman akan
   diperbaiki oleh Start; ownership ambiguous wajib diperiksa manusia.
6. Jika website menampilkan `SERVICE_UNAVAILABLE` tetapi status `ready`, periksa
   deployment Vercel secara terpisah tanpa menyalin secret.

## F. Checklist sebelum membagikan link

- Status terakhir adalah `state=ready`.
- `local_health=yes` dan `public_health=yes`.
- `listener_loopback=yes` dan `firewall_valid=yes`.
- ACL config dan pgpass bernilai `yes`.
- Backup tidak `stale` atau `missing`.
- Gunakan hanya link yang sudah disetujui; jangan menyalin link dari output
  diagnostik atau menyertakan token.

Larangan: jangan membuka port router, jangan mengekspos port 8000 langsung,
jangan edit pgpass manual, jangan mengubah database Production manual, dan
jangan membuat task agar Funnel publik aktif otomatis saat boot.
