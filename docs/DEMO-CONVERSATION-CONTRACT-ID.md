# Kontrak percakapan Demo AURA (offline hardening)

Scope: parser deterministik dan kelanjutan draft, bukan perubahan provider,
auth, schema DB, presisi waktu, atau janji kualitas generatif.

## Waktu

Oracle terpusat pengujian berada di `TIME_ORACLE` pada
`tests/test_demo_conversation_simulation.py`.

| Bentuk | Nilai kanonik |
|---|---|
| 8 pagi / 10 pagi | 08:00 / 10:00 |
| 11 siang / 11.30 siang | 11:00 / 11:30 |
| 12 siang / 1 siang / 2 siang / 3 siang | 12:00 / 13:00 / 14:00 / 15:00 |
| 3 sore / 7 malam / 11 malam | 15:00 / 19:00 / 23:00 |
| 12 malam / 12 am / 12 pm | 00:00 / 00:00 / 12:00 |
| 11 am / 11 pm / 11:30 pm | 11:00 / 23:00 / 23:30 |
| 00:00 / 23:59 | valid secara format |
| 24:00 / 25:00 / 12:60 / 8.6 malam | invalid; tidak diperbaiki diam-diam |
| jam 8 / 7 siang / dua waktu alternatif | minta input jelas; tidak memilih periode/nilai |

Qualifier diproses bersama jam/menit, bukan setelah hasil format numerik
dikembalikan. `siang` bukan sinonim PM. Oracle lama `7 siang = 19:00`
dikoreksi menjadi penolakan konservatif, sesuai izin kampanye untuk klarifikasi.
Nama/jumlah/tanggal/referensi bukan sumber jam hanya karena berisi angka.
`12 malam` tidak menggeser tanggal; domain tetap memvalidasi tanggal yang dipilih.

## Tanggal dan tahun

Tahun eksplisit tidak diubah. Domain menolak tanggal lampau. Parser historis
tetap memilih kejadian tahun berikutnya untuk tanggal bernama tanpa tahun yang
sudah lewat. Pada create maupun update, inferensi lintas tahun harus dilengkapi
pengguna dengan tanggal bertahun eksplisit sebelum lanjut ke konfirmasi/mutasi;
`ya` saja bukan input tanggal. Ini juga berlaku untuk edit tanggal pada tahap
konfirmasi create. Nama/jumlah/jam yang sudah valid tetap dipertahankan.
Contoh pada 5 September 2026: `2 Agustus` meminta tanggal, bulan, dan tahun;
tidak otomatis menjadi 2 Agustus 2027. `2 Agustus 2026` tetap ditolak sebagai
tanggal lampau; `2 Agustus 2027` diterima bila pengguna menyatakannya eksplisit.
Angka empat digit pada catatan lain bukan otorisasi tahun tanggal.

Tahun numerik/alphanumeric yang melekat pada tanggal harus utuh empat digit
dan valid: day-month, month-day, maupun DD/MM/YYYY dan DD-MM-YYYY tidak boleh
membuang token seperti 20266, 20x6, atau 026 lalu menginfer tahun lain.
Angka panjang pada catatan terpisah bukan token tahun tanggal.

Tanggal alternatif/konflik relative/weekday dengan tanggal absolut meminta
klarifikasi. Ini juga berlaku untuk dua DD-MM-YYYY, dan kalimat "6 September,
bukan 7 September": grammar saat ini tidak memilih tanggal lewat negasi.
Tanggal plus time/people yang tidak konflik tetap bisa diekstraksi, termasuk
"6 September 20:00". Pada create kedua komponen dikumpulkan; pada update date
hanya date berubah dan time persisted tetap, walaupun pesan menyebut jam lain.
Grammar relatif yang sudah ada (hari ini/besok/lusa/weekday)
tidak diperluas. `bulan depan` tetap membutuhkan tanggal lengkap.

AM/PM hanya qualifier jika melekat pada ekspresi jam: "I am booking at 11 pm"
berarti 23:00; "I am Dani" tidak berisi jam. Dua clock/qualifier yang bertentangan
tetap meminta klarifikasi. Locale request tidak diganti berdasarkan kata "am".
Jawaban angka jam saja saat create (misalnya `4`) meminta periode waktu atau
format 24 jam; tidak ditebak menjadi 04.00 atau 16.00. `jam 4 sore` = 16.00.
Tanggal baru divalidasi bersama jam yang sudah terkumpul sebelum konfirmasi.
Jika jam itu sudah lewat untuk tanggal baru, hanya jam yang dikosongkan dan
diminta ulang. Edit tanggal/jam pada konfirmasi (langsung maupun dua langkah)
juga memvalidasi pasangan tanggal-jam sebelum mengganti draft; input invalid
mempertahankan nilai lama dan gate editing, sehingga `ya` tidak melewati koreksi.

## Partial date dan lifecycle

`Tanggal 5` lalu `September 2026` digabung hanya dalam draft reservasi aktif.
Day dapat dikoreksi; kalender invalid tidak di-clamp. Jalur pemilihan field
update dan edit saat konfirmasi juga mempertahankan day.

`pending_reservation_day` adalah integer 1..31 (bukan bool), optional pada payload
workflow v2, hanya dalam create date draft atau update input_value/date. Payload
lama tetap valid; v1 tidak diperluas. Tidak ada kolom/migration aplikasi baru.
Rollback ke binary lama saat draft berfield baru masih aktif memerlukan perhatian:
reader lama fail-closed atas key baru, bukan jaminan backward deployment rollback.

Field ini masuk whitelist memory reservasi, sehingga restore/reset/expiry tidak
meninggalkannya sebagai general memory. Completion/cancel/intent switch relevan
membersihkannya. Durable blocker tidak menyimpan partial day. Reset menggunakan
owner/session contract lama dan tidak mengganti token sesi yang masih aktif.

## Mutasi dan recovery yang dipertahankan

Service tetap authoritative untuk direct API maupun chat. Setiap update termasuk
name/people harus menghasilkan kandidat temporal valid. Menit yang sama masih
boleh; tidak ada lead time baru. Read/cancel tidak diberi guard update baru.

Preflight → commit marker → row lock/reload → merge/revalidate → satu-field write
→ commit → respons snapshot commit operasi itu. Waktu dibandingkan saat final
validation di bawah lock, termasuk sesudah menunggu writer lain. Tidak ada janji
row akan selalu berada di masa depan setelah waktu berjalan.

Marker dan mutasi bukan satu transaksi atomik. Marker tetap durable saat mutasi
rollback; fresh restore memblokir replay yang tidak pasti. Tidak ada blind retry.
Nol mutasi reservasi setelah invalid input bukan berarti nol update marker jika
final revalidation gagal setelah marker sudah durable.

Locale tetap berasal dari request-scoped `SupportedLocale`, bukan ditebak dari
isi pesan. General conversation/prompt/provider policy tidak diubah. Respons
informatif dan fixture provider bukan bukti kualitas model live.
