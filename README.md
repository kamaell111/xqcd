# ZTE OLT C320 Manager — Dokumentasi & Alur Aplikasi

Dokumen ini menggabungkan `README.md` (dokumentasi project) dan `ALUR_APLIKASI.md` (alur data tahap demi tahap) jadi satu file. Sudah dikoreksi lewat cross-check ke `HANDOFF.md` — ada beberapa hal di dua dokumen asal yang salah, ketinggalan, atau kontradiksi satu sama lain.

![Version](https://img.shields.io/badge/version-1.0.0-blue)
![Python](https://img.shields.io/badge/python-3.13+-green)
![FastAPI](https://img.shields.io/badge/fastapi-0.118+-red)
![License](https://img.shields.io/badge/license-MIT-yellow)

---

## 📝 Catatan Koreksi (baca ini dulu)

Sembilan hal yang saya perbaiki, dan alasannya:

1. **Port aplikasi tidak konsisten.** README lama pakai `8000` di semua contoh (instalasi, API, curl, testing). Tapi `ALUR_APLIKASI.md` (TAHAP 1) dan `HANDOFF.md` sama-sama pakai `8001`. → Distandarkan ke **8001** di seluruh dokumen ini, karena itu yang benar-benar dipakai di instance kerja (`xqcd-lan/`). Kalau nanti mau publish sebagai template instalasi bersih, port itu boleh diganti — tapi jangan campur dua angka di dokumen yang sama.

2. **Interval auto-sync scheduler salah.** README lama bilang "tiap 60 detik" (`SNMP_POLL_INTERVAL=60`), tapi ALUR TAHAP 1 dan HANDOFF §6 sama-sama bilang scheduler `poll_olts` jalan tiap **30 detik**. → Dikoreksi ke 30 detik. Nilai `60` di `.env.example` kemungkinan sisa draft awal — cek isi `.env` yang jalan sekarang untuk pastikan.

3. **Daftar alert tidak lengkap.** README lama cuma sebut `onu_offline`, `pppoe_down`, `optical_low`, `cpu_high`, `mem_high`. HANDOFF §4 sebut ada alert terpisah **`onu_los`** (fiber putus, pesan beda dari offline biasa) dan alert info **"recovery"** (ONU pulih, ditandai biru). Dua-duanya hilang dari README lama. → Ditambahkan.

4. **Pola async job untuk provisioning tidak disebut sama sekali.** README lama (bagian API Endpoints & Testing) menjelaskan `POST /onu/provision` seolah request biasa — kirim, tunggu, dapat hasil langsung. Padahal menurut HANDOFF §6, endpoint ini sudah pakai pola **async job**: respons `202 Accepted` + `job_id` dalam ~27ms, status sebenarnya baru bisa dicek lewat `GET /jobs/{id}`. Kalau ikutin contoh curl README lama apa adanya, orang bakal bingung kenapa "hasilnya" cuma job_id, bukan detail ONU. → Endpoint `/jobs/{id}` ditambahkan, contoh curl & testing diperbaiki.

5. **File-file inti hilang dari "Struktur Project".** `trap_receiver.py`, `trap_sync.py`, `recovery_tracker.py`, `olt_manager.py` (lock per-OLT + job store), dan router `jobs.py` gak ada di daftar struktur README lama — padahal ini komponen paling penting (lock per-OLT dan SNMP trap real-time yang jadi pembeda utama aplikasi ini). → Ditambahkan.

6. **Fitur SNMP Trap gak disebut di bagian Fitur README.** Padahal ini salah satu fitur utama (detail lengkap di TAHAP 6 alur bawah). `snmp_client.py` di README lama malah ditandai "(opsional)" — ini membingungkan karena kemungkinan itu modul SNMP GET yang beda dari trap receiver (listener trap yang aktif jalan terus). → Fitur trap ditambahkan, dan dua hal ini dipisahkan biar gak ketuker.

7. **Kontradiksi soal auto-refresh 5 detik.** ALUR lama (TAHAP 4) bilang tiap 5 detik frontend manggil `loadUncfg()` → `POST /olts/1/sync-pons`, artinya Telnet ke OLT tiap 5 detik. Tapi HANDOFF §4 eksplisit bilang auto-refresh itu **"baca DB saja, bukan hit OLT langsung"**. Dua klaim ini gak mungkin benar bersamaan — `sync-pons` itu 10–15 command Telnet; kalau jalan tiap 5 detik terus-menerus, OLT bakal dibanjiri command dan gampang bentrok sama lock per-OLT tiap kali ada user lain nulis config. → Diikuti versi HANDOFF (lebih masuk akal secara desain): auto-refresh 5 detik = baca DB saja; sync ke OLT tetap di scheduler 30 detik + saat dashboard pertama kali dibuka. **Tapi ini perlu dicek ulang langsung ke `app.js`** — jangan percaya dokumen mentah-mentah, cek kodenya.

8. **Peringatan multi-worker di production (bug laten, belum pernah disebut di dokumen manapun).** README lama kasih contoh production `gunicorn app:app -w 2 ...` (2 worker process). Tapi lock per-OLT (`threading.Lock` di `olt_manager.py`) dan job store provisioning itu **in-memory**, hidup per-proses. Kalau jalan dengan lebih dari 1 worker: (a) dua worker bisa sama-sama pegang "lock" versinya sendiri-sendiri dan nulis ke OLT bersamaan tanpa saling tahu — race condition yang justru mau dicegah oleh locking ini; (b) request provision bisa masuk ke worker A, tapi polling `GET /jobs/{id}` nyasar ke worker B yang gak punya job itu di memori → 404 palsu. HANDOFF §9 sendiri sudah catat TODO "Job persistence — kalau nanti pakai multi-worker", yang menyiratkan **sekarang cuma aman jalan 1 worker**. → Contoh production diubah jadi `-w 1` dengan catatan tebal, sampai job store/lock dipindah ke sesuatu yang shared antar-proses (Redis, dll).

9. **Koreksi kecil lainnya:** label "SSH/Telnet Client" diperjelas jadi "Telnet Client" saja (project ini cuma pakai Telnet, sesuai driver Netmiko `zte_zxros_telnet`, bukan SSH). Tabel `config_versions` di README lama gak ada konfirmasinya di HANDOFF — ditandai **perlu verifikasi**. Begitu juga "role-based privilege (0–15)" — gak disebut di dokumen lain, ditandai **perlu verifikasi, mungkin belum aktif dipakai**.

> Kredensial asli (IP OLT, user/pass, community SNMP) sengaja **tidak** ditulis ulang di sini — itu dokumen privat, lihat `HANDOFF.md §14`. Dokumen ini pakai placeholder (`YOUR_OLT_IP`, dst) karena formatnya cocok buat di-push ke repo publik.

---

## Daftar Isi

- [Fitur](#fitur)
- [Arsitektur](#arsitektur)
- [Alur Kerja Sistem (6 Tahap)](#alur-kerja-sistem-6-tahap)
- [Instalasi](#instalasi)
- [Konfigurasi](#konfigurasi)
- [Cara Jalankan](#cara-jalankan)
- [API Endpoints](#api-endpoints)
- [Testing](#testing)
- [Struktur Project](#struktur-project)
- [Catatan Teknis Penting](#catatan-teknis-penting)
- [Troubleshooting](#troubleshooting)
- [Monitoring & Maintenance](#monitoring--maintenance)
- [Changelog](#changelog)
- [Lisensi](#lisensi)

---

## Fitur

**Monitoring & Sync (real dari OLT):**
- Sync CPU / Memory / Uptime
- Sync 16 PON port (status up/down)
- Sync daftar ONU lengkap (SN, name, VLAN, PPPoE)
- Deteksi ONU belum terdaftar (uncfg)
- Optical RX/TX per ONU
- Distance + online duration per ONU
- Status internet ONU (PPPoE connected/disconnected)
- Auto-sync background scheduler tiap **30 detik** *(dikoreksi dari "60 detik")*
- Chart CPU/Memory history

**SNMP Trap — event real-time dari OLT** *(baru ditambahkan, hilang dari README lama)*
- Listener UDP async di port 1620 (OLT kirim ke port 162, di-redirect iptables)
- Debounce 3–12 detik (cegah trap storm saat banyak ONU mati bareng)
- Trigger light sync (1–2 command Telnet) → update DB → evaluasi alert
- Prinsip: **trap = hint, Telnet = verifikasi, DB = otoritas** — trap gak pernah langsung dipercaya mentah-mentah
- *(Catatan: ini beda dari `snmp_client.py`/SNMP GET yang ditandai "opsional" — itu kemungkinan modul terpisah yang belum tentu aktif jalan; perlu diverifikasi)*

**Manajemen ONU:**
- Provision ONU baru dari UI (wizard 4 langkah) — **berjalan async**: request langsung dapat `job_id`, hasil sebenarnya dicek lewat polling status job
- Verifikasi otomatis ONU terdaftar di OLT + rollback otomatis kalau gagal
- Cek status PPPoE setelah provisioning (polling maks 30 detik)
- Hapus ONU (OLT + DB) dengan verifikasi — **synchronous**, bukan job async
- Reboot ONU — **synchronous**

**Manajemen Port:**
- Enable/disable PON port
- Enable/disable uplink port (gei / xgei)
- Konfirmasi sebelum eksekusi

**Manajemen VLAN:**
- Tambah / hapus VLAN dari UI → dikirim ke OLT + verifikasi — **synchronous**, dilindungi lock per-OLT
- Auto-sync dari OLT saat buka menu

**Alert System (auto-generate + auto-resolve):**
- `onu_offline` — ONU offline (critical)
- `onu_los` — fiber putus / loss of signal (critical, pesan beda dari offline biasa) *(hilang dari README lama — ditambahkan)*
- `pppoe_down` — PPPoE disconnect (warning)
- `optical_low` — RX lemah (warning/critical, threshold di `.env`)
- `cpu_high` — CPU tinggi (warning)
- `mem_high` — Memory tinggi (warning)
- Alert **recovery** — info biru "ONU X sudah pulih" *(hilang dari README lama — ditambahkan)*
- Anti-duplikat + auto-resolve + acknowledge manual + **bulk delete** (checkbox, hapus terpilih sekaligus) *(bulk delete belum disebut di README lama)*

**Audit Log:**
- Semua aksi user dicatat: login, create_vlan, provision_onu, toggle_port, dll
- Bulk delete audit log

**Security:**
- JWT authentication
- Password hashing (pbkdf2_sha256)
- Role-based privilege (0–15) — ⚠️ *belum terverifikasi di dokumen lain, cek apakah ini benar-benar dipakai atau sisa boilerplate*
- Audit trail
- **Lock per-OLT** (`threading.Lock`) di setiap endpoint write + scheduler — mencegah race condition antar-request maupun antara scheduler dan user yang lagi nulis config *(fitur inti yang hilang total dari README lama)*

---

## Arsitektur

```
Browser (Frontend)
  - Vanilla JS + Chart.js
  - Dashboard, ONU, PON, VLAN, Alert
                        │
                        │ HTTP/REST
                        ▼
┌───────────────────────────────────────────────────────┐
│               FASTAPI BACKEND (Python)                 │
│                                                         │
│  Routers:                                              │
│  ├── auth, olts, monitoring, sync                      │
│  ├── onu        (provision [async job] / delete /      │
│  │               reboot [synchronous])                 │
│  ├── ports      (enable / disable)                     │
│  ├── config     (running-config & VLAN)                │
│  ├── alerts, users                                      │
│  └── jobs       (GET /jobs/{id} — status async job)     │
│                                                         │
│  Background:                                           │
│  ├── Scheduler: poll_olts() tiap 30 detik               │
│  ├── Trap receiver: listen UDP 1620 (async)             │
│  └── Alert auto-generate + auto-resolve                 │
│                                                         │
│  Lock per-OLT (olt_manager.py) — wajib di semua write   │
│                                                         │
│  Database: SQLite (WAL Mode)                            │
│  ├── users, olts, pon_ports, onus                       │
│  ├── interfaces, vlans, alerts                          │
│  ├── metric_history, config_versions*                   │
│  │   (*belum terverifikasi ke HANDOFF)                  │
│  └── audit_logs                                          │
└─────────────────────────┬───────────────────────────────┘
                           │ Netmiko (Telnet, driver zte_zxros_telnet)
                           ▼
┌───────────────────────────────────────────────────────┐
│               OLT ZTE ZXA10 C320                        │
│               YOUR_OLT_IP:PORT (Telnet + enable password)│
└───────────────────────────────────────────────────────┘
                           ▲
                           │ SNMP Trap (UDP, push dari OLT)
                           │ port 162 → iptables REDIRECT → 1620
                           │
                  (ditangani trap_receiver.py, lihat TAHAP 6)
```

**Tech Stack:**

| Layer | Technology | Version |
|-------|-----------|---------|
| Backend | Python FastAPI | 0.118+ |
| Database | SQLite (WAL mode) | 3.x |
| Frontend | HTML5 + Vanilla JS | - |
| Charts | Chart.js | 4.4.0 |
| Icons | Font Awesome | 6.4.0 |
| Telnet Client | Netmiko (`zte_zxros_telnet`) | 4.5.0+ |
| Scheduler | APScheduler (asumsi — belum terverifikasi library-nya di HANDOFF) | 3.11+ |
| Auth | python-jose + passlib | - |

**Ringkasan Protokol:**

| Protokol | Antara | Port |
|---|---|---|
| HTTP | Browser ↔ FastAPI | 8001 |
| Telnet | FastAPI ↔ OLT (Netmiko) | 23 (LAN) / custom (publik) |
| SNMP Trap | OLT → FastAPI (push) | UDP 162 → 1620 (iptables redirect) |
| SQLAlchemy | FastAPI ↔ SQLite | (bukan protokol jaringan) |
| JWT | Token auth di header HTTP | — |

---

## Alur Kerja Sistem (6 Tahap)

Bagian ini menjelaskan bagaimana data mengalir dari aplikasi start sampai event push dari OLT.

### TAHAP 1 — Aplikasi Start

```
$ uvicorn app:app --host 0.0.0.0 --port 8001
                │
                ▼
    ┌──────────────────────────────┐
    │   FastAPI Start               │
    │   - Buka SQLite DB            │
    │   - Start Scheduler           │
    │   - Daftar job poll_olts (30s)│
    │   - Start Trap Receiver       │
    │     (listen UDP 1620)         │
    │   - Listen HTTP :8001         │
    └────────────┬─────────────────┘
                 │ (30 detik pertama)
                 ▼
    ┌──────────────────────────────┐
    │   Scheduler: poll_olts()      │
    └────────────┬─────────────────┘
                 │  Telnet (Netmiko)
                 ▼
          ┌──────────────┐
          │     OLT      │
          │  ZTE C320    │
          └──────┬───────┘
                 │ output: CPU/Mem/Uptime,
                 │ PON state, ONU list,
                 │ optical, PPPoE
                 ▼
    ┌──────────────────────────────┐
    │   FastAPI Parse + Save DB     │
    └──────────────────────────────┘
```

### TAHAP 2 — User Buka Browser

```
Buka: http://localhost:8001
                │
                ▼
    Load index.html + app.js + style.css
                │
                ▼
    Login: (lihat HANDOFF §14 untuk kredensial)
                │
                │  HTTP POST /api/v1/auth/login
                ▼
    FastAPI auth check → cek user di DB → generate JWT
                │
                │  HTTP response: {access_token, user}
                ▼
    Browser simpan token → localStorage
                │
                ▼
    loadPage("dashboard")
```

### TAHAP 3 — Dashboard Load Data

Frontend memicu 5 endpoint sekaligus (paralel) saat dashboard pertama kali dibuka:

```
loadDashboardStats() ──HTTP──▶ GET  /olts/1/status     (baca DB)
loadPONGrid()        ──HTTP──▶ GET  /olts/1/pons       (baca DB)
loadRecentAlerts()   ──HTTP──▶ GET  /alerts            (baca DB)
loadONUOptical()     ──HTTP──▶ GET  /olts/1/onus       (baca DB)
loadUncfg()          ──HTTP──▶ POST /olts/1/sync-pons  (SYNC KE OLT — 10-15 command Telnet)
```

4 endpoint baca DB langsung (cepat). 1 endpoint (`sync-pons`) benar-benar Telnet ke OLT, ambil lock per-OLT, parse, simpan DB, baru response.

**Multi-user:** kalau 2 user buka dashboard bersamaan, `loadUncfg()` di keduanya rebutan lock per-OLT — salah satu nunggu giliran, bukan dua-duanya nembak Telnet bersamaan.

### TAHAP 4 — Auto-Refresh (Tiap 5 Detik)

> ⚠️ **Ini bagian yang dikoreksi (lihat Catatan Koreksi #7).** Versi asli ALUR bilang tiap 5 detik ada `POST /olts/1/sync-pons` (hit Telnet). Versi ini mengikuti HANDOFF, yang bilang auto-refresh cuma baca DB. **Cek ulang ke `app.js` sebelum dipercaya penuh.**

```
Frontend setInterval(5000) — SEMUA baca DB, TIDAK hit Telnet langsung:
- loadDashboardStats()  ──HTTP──▶ GET /olts/1/status
- loadONUs()            ──HTTP──▶ GET /olts/1/onus
- pollRecoveries()      ──HTTP──▶ GET /alerts/recent-recoveries
- pollNewAlerts()       ──HTTP──▶ GET /alerts
- loadAlertBadge()      ──HTTP──▶ GET /alerts

Sync ke OLT yang sesungguhnya (Telnet) hanya terjadi di:
- Scheduler background tiap 30 detik (TAHAP 1)
- Saat dashboard pertama kali dibuka (TAHAP 3)
```

Kalau di halaman lain (ONU, VLAN, dll): cuma baca DB, tidak sync ke OLT.

### TAHAP 5 — User Aksi Synchronous (contoh: Tambah VLAN)

```
User isi wizard + klik "Simpan"
                │
                ▼  HTTP POST /api/v1/olts/1/vlans
                    Body: {vlan_id: 300, name:"X"}
                ▼
FastAPI (routers/config.py)
  - Validasi data
  - Ambil lock per-OLT
                │  Telnet
                ▼
OLT: configure terminal → vlan database →
     vlan 300 name X → exit → end → write
                │  OK
                ▼
FastAPI:
  - Verifikasi via show vlan
  - Simpan ke SQLite DB
  - Release lock
  - Audit log
                │  HTTP response JSON
                ▼
Browser: toast sukses + refresh tabel VLAN
```

Pola yang sama (synchronous, dilindungi lock) berlaku untuk: toggle port, hapus ONU, reboot ONU. **Bukan** untuk provisioning ONU baru — itu satu-satunya yang pakai job async (lihat §Catatan Koreksi #4 dan bagian API Endpoints).

### TAHAP 6 — SNMP Trap (Event Push dari OLT)

⚠️ Hanya jalan kalau aplikasi satu jaringan dengan OLT (LAN).

```
Event di OLT: ONU dicabut (dying gasp), fiber putus (LOS),
ONU recovery, warning lain (optical drop, dll)
                │
                ▼
OLT kirim SNMP Trap (UDP), port OLT → port 162 aplikasi
Payload ~200-300 byte, isi: eventId=N@eventLevel=LEVEL@[confirm@]timestamp
                │  UDP Packet
                ▼
Laptop (aplikasi): iptables REDIRECT port 162 → 1620
                │
                ▼
trap_receiver.py (asyncio UDP, listen 1620)
  - Regex: eventid=(\d+)@eventlevel=(\w+)  — IGNORECASE
    (ZTE pakai mixed-case eventId/eventLevel)
                │
                ▼
DEBOUNCE (3–12 detik)
  - Trap baru datang → reset timer 3 detik
  - 3 detik sepi → trigger sync
  - Max wait 12 detik (force trigger)
  - Tujuan: cegah trap storm (100 ONU mati bareng = ratusan trap)
                │
                ▼
trap_sync.py → light sync (1-2 command Telnet)
  - show gpon onu state
  - Update status ONU di DB
  - Kalau ONU online kembali → poll PPPoE (maks 30 detik)
                │
                ▼
alerting.py → auto-generate alert
  - LOS → critical | offline → critical | recovery → info
  - Auto-resolve kalau pulih
                │  Update SQLite DB
                ▼
Frontend polling 5 detik:
  - pollNewAlerts() → toast merah
  - pollRecoveries() → toast hijau
  - loadAlertBadge() → update badge
                │
                ▼
Browser: toast notifikasi + Alert Center + status ONU di tabel update
```

**Prinsip:** trap = hint, bukan sumber kebenaran. Setelah trap masuk, aplikasi tetap verifikasi via Telnet sebelum update DB.

**Cek Trap Jalan:**

```bash
# 1. Listener
sudo ss -ulnp | grep 1620
# Harus ada uvicorn listen di 0.0.0.0:1620 (bukan 127.0.0.1)

# 2. iptables rule
sudo iptables -t nat -L PREROUTING -n | grep 162

# 3. Counter naik saat OLT kirim trap
sudo iptables -t nat -L PREROUTING -n -v | grep 162

# 4. Log uvicorn — ada baris [TRAP] dari IP OLT
```

**Ringkasan 6 Tahap:**

| Tahap | Trigger | Aksi | Protokol |
|---|---|---|---|
| 1. Start | `uvicorn app:app` | Start scheduler + trap receiver | — |
| 2. Login | User buka browser | Auth → JWT | HTTP |
| 3. Dashboard load | User login | Sync PON/ONU + baca DB | HTTP + Telnet |
| 4. Auto-refresh | Auto tiap 5s | Baca DB saja | HTTP |
| 4b. Scheduler | Auto tiap 30s | Sync lengkap ke OLT | Telnet |
| 5. User aksi | Klik wizard | Kirim ke OLT + simpan DB (lock) | HTTP + Telnet |
| 6. SNMP Trap | Event OLT | Push → debounce → verifikasi → alert | SNMP Trap + Telnet |

---

## Instalasi

### Prasyarat

| Komponen | Minimum | Rekomendasi |
|----------|---------|-------------|
| OS | Ubuntu 22.04+ | Ubuntu 24.04 LTS |
| Python | 3.10+ | 3.13 |
| RAM | 512 MB | 1+ GB |
| Storage | 500 MB | 2+ GB |
| Akses OLT | Telnet IP:port | - |

### Langkah

```bash
# 1. Clone repository
git clone https://github.com/USERNAME/xqcd.git
cd xqcd

# 2. Buat virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 3. Install dependencies
pip install --upgrade pip
cd backend
pip install -r requirements.txt

# 4. Setup .env (copy dari template)
cp .env.example .env
nano .env    # isi SECRET_KEY, kredensial OLT, dll

# 5. Jalankan
uvicorn app:app --reload --host 0.0.0.0 --port 8001
```

### Akses

| URL | Deskripsi |
|-----|-----------|
| `http://localhost:8001` | Dashboard |
| `http://localhost:8001/docs` | Swagger UI (auto-generate) |
| `http://localhost:8001/redoc` | ReDoc API docs |

Login: buat user baru saat pertama setup, atau ubah di `app.py` (`seed_data`).

---

## Konfigurasi

Semua konfigurasi via file `backend/.env`. Copy dari `.env.example`:

```env
# Aplikasi
APP_NAME=ZTE OLT Manager
SECRET_KEY=change-me-generate-with-secrets-token-hex-32
ACCESS_TOKEN_EXPIRE_MINUTES=60

# Database
DATABASE_URL=sqlite:///./data/zte_olt_manager.db

# Scheduler
SNMP_POLL_INTERVAL=30

# Alert Thresholds
ALERT_CPU_WARNING=85.0
ALERT_MEM_WARNING=90.0
ALERT_OPTICAL_RX_WARNING=-25.0
ALERT_OPTICAL_RX_CRITICAL=-28.0
ALERT_AUTO_RESOLVE=True
```

**Generate SECRET_KEY:**

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

---

## Cara Jalankan

### Development

```bash
cd ~/Downloads/xqcd-lan/backend
source ~/Downloads/xqcd-lan/.venv/bin/activate
uvicorn app:app --reload --host 0.0.0.0 --port 8001
```

### Production (Gunicorn + Uvicorn Worker)

> ⚠️ **Wajib 1 worker sampai job store & lock per-OLT dipindah ke storage shared (mis. Redis).** Lock (`threading.Lock`) dan job store provisioning saat ini in-memory per-proses. Dengan >1 worker, dua proses bisa nulis ke OLT yang sama tanpa saling tahu (lock jadi percuma), dan polling `GET /jobs/{id}` bisa nyasar ke worker yang gak punya job itu di memori. Lihat Catatan Koreksi #8.

```bash
cd backend
pip install gunicorn
gunicorn app:app -w 1 -k uvicorn.workers.UvicornWorker \
    --bind 0.0.0.0:8001 --access-logfile -
```

---

## API Endpoints

Base URL: `http://localhost:8001/api/v1`

### Auth

| Endpoint | Method | Auth | Fungsi |
|----------|--------|------|--------|
| `/auth/login` | POST | None | Login → JWT |
| `/auth/me` | GET | JWT | Info user aktif |

### OLT & Monitoring

| Endpoint | Method | Fungsi |
|----------|--------|--------|
| `/olts` | GET | List OLT |
| `/olts/{id}/status` | GET | Status CPU/Mem/Uptime |
| `/olts/{id}/sync` | POST | Sync CPU/Mem/Uptime |
| `/olts/{id}/sync-pons` | POST | Sync 16 PON + ONU + optical + PPPoE |
| `/olts/{id}/pons` | GET | List PON |
| `/olts/{id}/onus` | GET | List ONU |
| `/olts/{id}/onus/{onu_id}` | GET | Detail ONU |
| `/olts/{id}/interfaces` | GET | List uplink |
| `/olts/{id}/metrics?metric=cpu` | GET | History CPU |

### Provisioning ONU (⚠️ ASYNC)

| Endpoint | Method | Fungsi |
|----------|--------|--------|
| `/onu/provision` | POST | Daftarkan ONU baru — **balikin `202` + `job_id` dalam ~27ms, BUKAN hasil langsung** |
| `/jobs/{id}` | GET | Cek status job (`queued` / `running` / `success` / `failed`) — *endpoint ini hilang dari README lama* |
| `/onu/{olt_id}/{onu_id}/reboot` | POST | Reboot ONU — synchronous |
| `/onu/{olt_id}/{onu_id}` | DELETE | Hapus ONU — synchronous |

### Port

| Endpoint | Method | Fungsi |
|----------|--------|--------|
| `/olts/{id}/ports/toggle` | POST | Enable/disable port |

### VLAN

| Endpoint | Method | Fungsi |
|----------|--------|--------|
| `/olts/{id}/vlans` | GET | List VLAN (auto-sync) |
| `/olts/{id}/vlans` | POST | Create VLAN |
| `/olts/{id}/vlans/{vlan_id}` | DELETE | Hapus VLAN |

### Alert

| Endpoint | Method | Fungsi |
|----------|--------|--------|
| `/alerts` | GET | List alert |
| `/alerts/{id}/ack` | POST | Acknowledge |
| `/alerts/{id}/resolve` | POST | Resolve |
| `/alerts/{id}` | DELETE | Hapus 1 alert |
| `/alerts/bulk-delete` | POST | Hapus alert terpilih sekaligus *(disebut di HANDOFF, hilang dari README lama)* |
| `/alerts/recent-recoveries` | GET | Alert recovery terbaru (untuk toast hijau) |

### Contoh Request

**Login:**

```bash
curl -X POST http://localhost:8001/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"YOUR_USER","password":"YOUR_PASS"}'
```

**Sync dari OLT:**

```bash
TOKEN="<paste-token-dari-login>"

curl -X POST http://localhost:8001/api/v1/olts/1/sync-pons \
  -H "Authorization: Bearer $TOKEN"
```

**Provision ONU baru (async — dikoreksi):**

```bash
TOKEN="<paste-token-dari-login>"

# Step 1: kirim request, dapat job_id (bukan hasil final)
curl -X POST http://localhost:8001/api/v1/onu/provision \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "olt_id": 1,
    "pon_port": "1/1/1",
    "onu_id": 3,
    "serial_number": "YYKC375F0511",
    "name": "CLIENT-ZTE-NEW",
    "onu_type": "F609",
    "tcont_profile": "1G",
    "tcont_name": "PON1",
    "gemport_id": 1,
    "traffic_limit": "1G",
    "service_port": 3,
    "vport": 1,
    "user_vlan": 15,
    "vlan": 15,
    "pppoe_user": "router1",
    "pppoe_password": "router1",
    "enable_nat": true
  }'
# Response: {"job_id": "...", "status": "queued"}  (HTTP 202)

# Step 2: polling status job sampai success/failed
JOB_ID="<job_id-dari-step-1>"
curl http://localhost:8001/api/v1/jobs/$JOB_ID \
  -H "Authorization: Bearer $TOKEN"
```

---

## Testing

**Test 1: Login**

```bash
curl -s -X POST http://localhost:8001/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"YOUR_USER","password":"YOUR_PASS"}' | python3 -m json.tool
```

**Test 2: Sync PON + ONU**

```bash
TOKEN="<paste-token-dari-login>"

curl -s -X POST http://localhost:8001/api/v1/olts/1/sync-pons \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

**Test 3: List ONU**

```bash
curl -s http://localhost:8001/api/v1/olts/1/onus \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

**Test 4: Provision ONU baru (async — dikoreksi)**

Kirim request seperti contoh di [Contoh Request](#contoh-request), simpan `job_id` dari response. Poll `GET /jobs/{job_id}` sampai status `success` atau `failed` — **jangan asumsikan sukses cuma karena request pertama balik 202**. Baru setelah status `success`, cek ONU muncul di `/olts/{id}/onus` dan status PPPoE-nya `connected`.

---

## Struktur Project

```
xqcd/
│
├── backend/
│   ├── app.py                    FastAPI + scheduler + seed + trap receiver startup
│   ├── olt_client.py             Client Telnet ZTE ZXAN (connection cache TTL 4 menit)
│   ├── olt_manager.py            Lock per-OLT (threading.Lock) + job store in-memory  ← hilang di README lama
│   ├── zxan_parser.py            Parser running-config + generator config ONU
│   ├── snmp_client.py            SNMP client (opsional/GET — beda dari trap receiver, perlu verifikasi status)
│   ├── trap_receiver.py          SNMP Trap receiver, asyncio UDP port 1620            ← hilang di README lama
│   ├── trap_sync.py              Light sync dipicu trap                              ← hilang di README lama
│   ├── recovery_tracker.py       Track recovery untuk notifikasi user (TTL 60s)       ← hilang di README lama
│   ├── alerting.py               Auto-generate + auto-resolve alert
│   ├── models.py                 SQLAlchemy models
│   ├── schemas.py                Pydantic schemas
│   ├── auth.py                   JWT + password hashing + audit
│   ├── database.py               SQLAlchemy engine + session
│   ├── config.py                 Settings dari .env (threshold alert + seed data)
│   ├── requirements.txt          Python dependencies
│   ├── .env.example              Template konfigurasi
│   ├── routers/
│   │   ├── auth.py               Login, /me
│   │   ├── olts.py               CRUD OLT
│   │   ├── monitoring.py         Status, PON, ONU, metrics
│   │   ├── sync.py               Sync CPU/PON/ONU/optical/PPPoE
│   │   ├── onu.py                Provision (async job) / reboot / hapus ONU
│   │   ├── ports.py              Enable/disable port PON & uplink
│   │   ├── config.py             Running-config & VLAN
│   │   ├── alerts.py             Alert management + bulk delete
│   │   ├── users.py              User + audit log + bulk delete audit
│   │   └── jobs.py               GET /jobs/{id}                                      ← hilang di README lama
│   └── data/
│       └── .gitkeep              Placeholder (DB tidak di-commit)
│
├── frontend/
│   ├── index.html                UI utama
│   ├── app.js                    Logic UI
│   └── style.css                 Styling
│
├── .gitignore
├── .dockerignore
├── LICENSE
└── README.md
```

---

## Catatan Teknis Penting

### 1. Netmiko via Telnet ZXAN → pakai `send_command_timing`

`send_config_set` sering timeout di ZXAN. Semua command dikirim satu per satu pakai `send_command_timing` dengan `last_read` disesuaikan (0.3 untuk output pendek, 1.0 untuk output panjang).

### 2. `configure terminal` wajib sebelum config mode

ZXAN tidak auto-enter config mode dari `interface X`. Command list harus:

```
configure terminal
interface gpon-olt_X
...
end
write
```

### 3. `vport` di ZXAN = per-ONU

`vport` selalu `1` per ONU. Yang unique cuma `service-port` number. Kalau `vport` di-set selain 1 → error `Invalid port`.

### 4. `service-port` tidak boleh nabrak

- ONU 1 → `service-port 1`
- ONU 3 → `service-port 3`
- Kalau nabrak → ONU lain bisa disconnect

### 5. Verifikasi wajib setelah write

Setiap operasi write (provision, delete, toggle port, VLAN) harus diverifikasi dari OLT sebelum simpan DB.

### 6. Timezone

Backend simpan UTC. API return timestamp dengan `+ "Z"` supaya browser auto-convert ke waktu lokal.

### 7. Huawei HG8145V5 tidak bisa provisioning via OLT ZTE

OMCI ZTE proprietary → Huawei tidak kenal ME-nya. Command yang gagal: `pppoe ... nat enable`, `firewall enable level low anti-hack disable`, `security-mgmt ...`, `wan 1 service internet host 1`. **Solusi:** ganti ke ONU ZTE F609 (100% kompatibel).

### 8. Lock per-OLT — wajib, bukan opsional

`threading.Lock` per OLT (`olt_manager.py`), dipakai di semua endpoint write **dan** scheduler (scheduler skip siklus kalau OLT lagi sibuk). Ini yang mencegah race condition antar-request maupun antara scheduler vs user yang lagi nulis config. **Jangan hilangkan ini walau "cuma butuh cepat".**

### 9. Async job pattern — hanya untuk provisioning

`POST /onu/provision` → 202 + `job_id` (~27ms). Background task jalan di thread terpisah, lock **dilepas** saat sleep/polling supaya OLT tetap bisa dipakai user lain. Idempotency via `create_job_atomic`. Operasi write lain (VLAN, toggle port, delete/reboot ONU) tetap synchronous.

### 10. Connection cache

`_CONN_CACHE` di `olt_client.py`, TTL 4 menit — sync ke-2 dst pakai koneksi existing (lebih cepat dari re-connect). Idle timeout Telnet OLT 15 menit, lebih panjang dari TTL cache, jadi aman dari koneksi zombie.

### 11. Trap = hint, bukan sumber kebenaran

Trap SNMP cuma memicu light sync (1-2 command verifikasi via Telnet). DB baru diupdate setelah verifikasi, bukan langsung dari isi trap.

---

## Troubleshooting

### "Pattern not detected" dari Netmiko

**Solusi:** Pakai `send_command_timing` per command (sudah dilakukan di `olt_client.py`).

### Provisioning sukses tapi ONU tidak ada di OLT

**Solusi:** Cek `zxan_parser.py` — pastikan ada `configure terminal` di awal + `end` + `write` di akhir. Cek juga status job lewat `GET /jobs/{id}` — kalau `failed`, harusnya sudah ada rollback otomatis.

### Alert tidak muncul

**Solusi:** Cek threshold di `.env` (atau `config.py`). Cek scheduler jalan di log (cari penanda auto-sync).

### VLAN tidak muncul di OLT

**Solusi:** Cek `routers/config.py` — command harus pakai `vlan database` → `vlan N name X` → `exit`.

### Distance ONU masih angka lama

**Solusi:** Klik Sync ulang (trigger `sync-pons` manual).

### OLT tidak bisa diakses

**Solusi:** Cek telnet `IP:port` bisa dijangkau. Cek kredensial di `.env`. Cek enable password.

### Login gagal terus

**Solusi:** Cek apakah user sudah ada di DB. Reset DB (hapus `backend/data/*.db`) akan seed ulang user default.

### Trap tidak masuk ke app

**Solusi:** Cek `sudo ss -ulnp | grep 1620` — harus `0.0.0.0:1620`, bukan `127.0.0.1:1620`. Cek iptables rule (hilang setelah reboot, harus di-set ulang manual — lihat §Cara Jalankan). Cek regex trap pakai `re.IGNORECASE` (ZTE pakai `eventId` mixed-case).

### LOS auto-resolve butuh 2x cabut-colok untuk update UI

Belum terdiagnosis penuh — dugaan sementara cache browser, **jangan dianggap sudah pasti** sampai dikonfirmasi.

---

## Monitoring & Maintenance

### Backup Manual

```bash
cp backend/data/zte_olt_manager.db \
   backup/zte_olt_manager_$(date +%Y%m%d_%H%M).db
```

### Backup Full Project (zip)

```bash
zip -r xqcd-backup-$(date +%Y%m%d_%H%M).zip . \
  -x ".venv/*" -x "venv/*" -x "**/__pycache__/*" \
  -x "**/*.pyc" -x "backend/data/*.db*" -x "backup/*"
```

### Cek Log Scheduler

```bash
# Development
uvicorn app:app --reload --host 0.0.0.0 --port 8001 | grep -i sync

# Production (Gunicorn access log)
tail -f access.log
```

---

## Changelog

### v1.0.0 - Initial Release

**Added:**

- Monitoring real-time CPU / Memory / Uptime dari OLT
- Sync 16 PON port + daftar ONU lengkap (SN, name, VLAN, PPPoE)
- Deteksi ONU belum terdaftar (uncfg)
- Optical RX/TX, distance, dan online duration per ONU
- Auto-sync background scheduler tiap 30 detik + chart history
- Wizard provisioning ONU 4 langkah, berjalan **async** (job pattern) dengan verifikasi & rollback otomatis
- Reboot dan hapus ONU (OLT + DB) dengan verifikasi
- Enable/disable PON port dan uplink port
- Manajemen VLAN (tambah/hapus) dengan verifikasi ke OLT
- Alert system auto-generate + auto-resolve (`onu_offline`, `onu_los`, `pppoe_down`, `optical_low`, `cpu_high`, `mem_high`) + alert recovery info
- Bulk delete untuk alert & audit log
- SNMP Trap receiver real-time (debounce + light sync)
- Lock per-OLT untuk semua operasi write
- Audit log untuk semua aksi user
- JWT authentication, password hashing, role-based privilege (⚠️ perlu verifikasi status pemakaian)

> Status sesungguhnya per catatan terakhir: **~85% selesai**, sinkronisasi ke GitHub masih tertunda (folder repo `xqcd/` masih versi lama dibanding `xqcd-lan/` yang aktif dikerjakan).

---

## Lisensi

MIT — lihat file [LICENSE](LICENSE).

## Kontribusi

Pull request welcome. Untuk perubahan besar, buka issue dulu untuk diskusi.
