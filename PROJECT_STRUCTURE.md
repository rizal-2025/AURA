# Struktur Project AURA

Dokumen ini menjelaskan struktur dan arsitektur project AURA secara lengkap.

## 1. Gambaran Umum

AURA adalah project FastAPI untuk membangun AI Restaurant Reservation Agent. Project ini menggabungkan:

- FastAPI sebagai framework web
- SQLAlchemy sebagai ORM
- PostgreSQL sebagai database
- Ollama / OpenAI Responses API sebagai provider AI
- Memori sesi untuk menjaga state percakapan

Tujuan utamanya adalah menerima pesan user, mengidentifikasi intent, mengekstrak informasi reservasi, lalu menyimpan data reservasi ke database.

---

## 2. Struktur Folder Utama

```text
AURA/
├── app/
│   ├── agents/
│   ├── api/
│   ├── core/
│   ├── db/
│   ├── dependencies/
│   ├── main.py
│   ├── memory/
│   ├── schemas/
│   ├── services/
│   └── utils/
├── tests/
├── create_tables.py
├── requirements.txt
├── struktur.txt
├── .env
└── PROJECT_STRUCTURE.md
```

---

## 3. Penjelasan Tiap Bagian

### 3.1 Folder app/
Folder utama aplikasi backend.

#### app/main.py
File entry point aplikasi FastAPI.

Fungsinya:
- membuat instance FastAPI
- mengatur title dan version aplikasi
- mendaftarkan router API
- menyediakan endpoint root dan health check

#### app/api/
Berisi endpoint HTTP.

- app/api/chat.py
  - endpoint untuk dialog chat dengan AI
  - menerima request user dan memanggil orchestrator

- app/api/reservation.py
  - endpoint untuk membuat reservasi secara langsung melalui API

#### app/agents/
Berisi orchestrator yang menjadi pusat alur logika aplikasi.

- app/agents/orchestrator.py
  - mengatur urutan pemrosesan user message
  - memeriksa intent
  - mengekstrak data reservasi
  - mengelola memori sesi
  - menyimpan data reservasi ke database

#### app/core/
Berisi komponen inti aplikasi.

- app/core/config.py
  - membaca konfigurasi dari environment / .env
  - menyimpan setting aplikasi seperti nama aplikasi, versi, URL database, dan provider AI

- app/core/logger.py
  - menyediakan logging untuk aplikasi

#### app/db/
Berisi semua hal terkait database.

- app/db/base.py
  - base class untuk declarative SQLAlchemy models

- app/db/database.py
  - membuat engine SQLAlchemy
  - membuat session factory
  - menyediakan dependency get_db untuk FastAPI

- app/db/models/
  - model ORM aplikasi
  - saat ini terdapat model Reservation

- app/db/repositories/
  - layer repository untuk operasi database
  - memisahkan logika penyimpanan dari service layer

#### app/memory/
Berisi state percakapan sementara.

- app/memory/session.py
  - menyimpan data sesi user dalam memori (in-memory)
  - digunakan untuk menjaga informasi yang masih belum lengkap selama percakapan

#### app/schemas/
Berisi model request/response menggunakan Pydantic.

- app/schemas/chat.py
  - schema untuk request dan response chat

- app/schemas/reservation.py
  - schema untuk input/output reservasi

#### app/services/
Berisi business logic aplikasi.

##### app/services/ai/
- app/services/ai/base.py
  - interface / abstract base class untuk provider AI

- app/services/ai/factory.py
  - memilih provider AI yang dipakai

- app/services/ai/ollama_provider.py
  - implementasi provider AI menggunakan Ollama

- app/services/ai/openai_provider.py
  - implementasi provider AI menggunakan OpenAI Responses API

##### app/services/intent/
- app/services/intent/classifier.py
  - mengklasifikasikan pesan user menjadi intent tertentu

- app/services/intent/service.py
  - wrapper service untuk classifier

##### app/services/reservation/
- app/services/reservation/extractor.py
  - mengekstrak informasi reservasi dari pesan user

- app/services/reservation/service.py
  - service layer untuk membuat reservasi

##### app/services/conversation/
- app/services/conversation/state.py
  - mengatur field yang wajib dilengkapi untuk proses reservasi
  - menghasilkan pertanyaan follow-up jika data belum lengkap

#### app/utils/
Berisi helper yang dipakai secara umum.

- app/utils/datetime_parser.py
  - memparse ekspresi waktu dan tanggal seperti hari ini, besok, jam 7 malam

---

## 4. Alur Request dari User ke AI

### A. Melalui endpoint chat
1. User mengirim request ke endpoint /chat.
2. Request masuk ke router [app/api/chat.py](app/api/chat.py).
3. Router memanggil AgentOrchestrator.
4. Orchestrator mengecek sesi user melalui memory.
5. Jika intent belum ada, sistem memanggil IntentClassifier.
6. IntentClassifier meminta AI untuk menilai apakah pesan termasuk reservation, general, promo, faq, complaint, atau lainnya.
7. Jika intent adalah reservation, sistem memanggil ReservationExtractor.
8. ReservationExtractor meminta AI untuk mengekstrak data seperti:
   - nama
   - jumlah orang
   - tanggal
   - jam
9. Data yang didapat dipadukan dengan parser tanggal dan waktu.
10. Sistem mengecek apakah semua field reservasi sudah lengkap.
11. Jika belum lengkap, sistem mengembalikan pertanyaan lanjutan.
12. Jika sudah lengkap, data disimpan ke database.
13. Hasil reservasi dikembalikan ke user.

### B. Melalui endpoint reservasi manual
1. User atau client memanggil endpoint /reservation.
2. Data reservasi diterima melalui schema ReservationCreate.
3. Data dikirim ke ReservationService.
4. ReservationService memanggil repository.
5. Repository menyimpan data ke PostgreSQL.

---

## 5. Alur Data ke Database

Flow data reservasi:

```text
API Router
  -> Service
  -> Repository
  -> SQLAlchemy Model
  -> PostgreSQL
```

Data yang disimpan biasanya meliputi:
- name
- people
- date
- time
- status

---

## 6. Komponen Penting dan Fungsinya

### FastAPI Router
- Menangani HTTP request
- Menghubungkan input user dengan logika aplikasi

### Orchestrator
- Mengatur alur kerja aplikasi
- Bertindak sebagai penghubung antar komponen

### AI Provider
- Menyediakan interface ke model AI
- Mendukung Ollama dan OpenAI Responses API

### Intent Classifier
- Menentukan maksud user

### Reservation Extractor
- Menentukan data reservasi dari percakapan

### Conversation State
- Menjaga kelengkapan data reservasi

### Repository
- Menyimpan data ke PostgreSQL

---

## 7. Struktur Database

Saat ini project menggunakan tabel berikut:

### Tabel reservations
Kolom utama:
- id
- name
- people
- date
- time
- status

Model tabel ini didefinisikan di [app/db/models/reservation.py](app/db/models/reservation.py).

---

## 8. Konfigurasi Aplikasi

Konfigurasi aplikasi diambil dari file .env melalui [app/core/config.py](app/core/config.py).

Konfigurasi yang umum dipakai:
- APP_NAME
- VERSION
- AI_PROVIDER
- OLLAMA_BASE_URL
- OLLAMA_MODEL
- OPENAI_MODEL
- DATABASE_URL
- OPENAI_API_KEY

---

## 9. File Pendukung di Root

### create_tables.py
File untuk membuat tabel database berdasarkan model SQLAlchemy.

### requirements.txt
Berisi dependency project.

### struktur.txt
File tekstual yang mendokumentasikan struktur folder secara sederhana.

### tests/
Berisi file uji coba dan pengujian sederhana.

---

## 10. Kelebihan Struktur Saat Ini

- Pemisahan concern cukup jelas antara API, service, DB, dan AI
- Mudah dipahami untuk project kecil-menengah
- Mengikuti pola yang umum pada aplikasi FastAPI

---

## 11. Potensi Kekurangan Struktur

Walau cukup rapi, ada beberapa area yang bisa diperbaiki di masa depan:

- Logic bisnis masih terpusat di orchestrator
- Memory menggunakan in-memory dictionary, sehingga tidak cocok untuk production multi-instance
- Belum ada layer service yang lebih kuat untuk chat flow
- Error handling dan validasi domain masih sederhana

---

## 12. Ringkasan Singkat

AURA adalah project FastAPI yang menggabungkan:
- API layer untuk menerima input user
- Agent orchestrator untuk mengatur alur percakapan
- AI provider untuk klasifikasi intent dan ekstraksi data
- SQLAlchemy + PostgreSQL untuk penyimpanan data reservasi

Project ini sudah memiliki struktur dasar yang baik untuk prototype atau MVP AI reservation assistant.
