# ZTE OLT Manager

Aplikasi web untuk monitoring & manajemen OLT **ZTE ZXA10 C320** via Telnet. Backend FastAPI + SQLite, frontend vanilla JS.

![Version](https://img.shields.io/badge/version-1.0.0-blue)
![Python](https://img.shields.io/badge/python-3.13+-green)
![FastAPI](https://img.shields.io/badge/fastapi-0.118+-red)
![License](https://img.shields.io/badge/license-MIT-yellow)

---

## Daftar Isi

- [Fitur](#fitur)
- [Arsitektur](#arsitektur)
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
- Auto-sync background tiap 60 detik
- Chart CPU/Memory history

**Manajemen ONU:**
- Provision ONU baru dari UI (wizard 4 langkah)
- Verifikasi otomatis ONU terdaftar di OLT
- Cek status PPPoE setelah provisioning
- Hapus ONU (OLT + DB) dengan verifikasi
- Reboot ONU

**Manajemen Port:**
- Enable/disable PON port
- Enable/disable uplink port (gei / xgei)
- Konfirmasi sebelum eksekusi

**Manajemen VLAN:**
- Tambah / hapus VLAN dari UI → dikirim ke OLT + verifikasi
- Auto-sync dari OLT saat buka menu

**Alert System (auto-generate + auto-resolve):**
- `onu_offline` — ONU tidak online (critical)
- `pppoe_down` — PPPoE disconnect (warning)
- `optical_low` — RX < -25 dBm
- `cpu_high` — CPU > 85%
- `mem_high` — Memory > 90%
- Anti-duplikat + auto-resolve + acknowledge manual

**Audit Log:**
- Semua aksi user dicatat: login, create_vlan, provision_onu, toggle_port

**Security:**
- JWT authentication
- Password hashing (pbkdf2_sha256)
- Role-based privilege (0-15)
- Audit trail

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
│  ├── onu        (provision / delete / reboot)          │
│  ├── ports      (enable / disable)                     │
│  ├── config     (running-config & VLAN)                │
│  └── alerts, users                                      │
│                                                         │
│  Background: APScheduler                               │
│  ├── Auto-sync CPU/Mem tiap 60 detik                    │
│  └── Alert auto-generate                                │
│                                                         │
│  Database: SQLite (WAL Mode)                            │
│  ├── users, olts, pon_ports, onus                       │
│  ├── interfaces, vlans, alerts                          │
│  ├── metric_history, config_versions                    │
│  └── audit_logs                                          │
└─────────────────────────┬───────────────────────────────┘
                           │ Netmiko (Telnet)
                           ▼
┌───────────────────────────────────────────────────────┐
│               OLT ZTE ZXA10 C320                        │
│               YOUR_OLT_IP:PORT (Telnet + enable password)│
└───────────────────────────────────────────────────────┘
```

**Tech Stack:**

| Layer | Technology | Version |
|-------|-----------|---------|
| Backend | Python FastAPI | 0.118+ |
| Database | SQLite (WAL mode) | 3.x |
| Frontend | HTML5 + Vanilla JS | - |
| Charts | Chart.js | 4.4.0 |
| Icons | Font Awesome | 6.4.0 |
| SSH/Telnet Client | Netmiko | 4.5.0+ |
| Scheduler | APScheduler | 3.11+ |
| Auth | python-jose + passlib | - |

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
uvicorn app:app --reload --host 0.0.0.0 --port 8000
```

### Akses

| URL | Deskripsi |
|-----|-----------|
| `http://localhost:8000` | Dashboard |
| `http://localhost:8000/docs` | Swagger UI (auto-generate) |
| `http://localhost:8000/redoc` | ReDoc API docs |

Login default: buat user baru saat pertama setup, atau ubah di `app.py` (`seed_data`).

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
SNMP_POLL_INTERVAL=60

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
cd ~/Downloads/xqcd
source .venv/bin/activate
cd backend
uvicorn app:app --reload --host 0.0.0.0 --port 8000
```

### Production (Gunicorn + Uvicorn Worker)

```bash
cd backend
pip install gunicorn
gunicorn app:app -w 2 -k uvicorn.workers.UvicornWorker \
    --bind 0.0.0.0:8000 --access-logfile -
```

---

## API Endpoints

Base URL: `http://localhost:8000/api/v1`

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

### Provisioning ONU

| Endpoint | Method | Fungsi |
|----------|--------|--------|
| `/onu/provision` | POST | Daftarkan ONU baru |
| `/onu/{olt_id}/{onu_id}/reboot` | POST | Reboot ONU |
| `/onu/{olt_id}/{onu_id}` | DELETE | Hapus ONU |

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

### Contoh Request

**Login:**

```bash
curl -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"YOUR_USER","password":"YOUR_PASS"}'
```

**Sync dari OLT:**

```bash
TOKEN="<paste-token-dari-login>"

curl -X POST http://localhost:8000/api/v1/olts/1/sync-pons \
  -H "Authorization: Bearer $TOKEN"
```

**Provision ONU baru:**

```bash
curl -X POST http://localhost:8000/api/v1/onu/provision \
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
```

---

## Testing

**Test 1: Login**

```bash
curl -s -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"YOUR_USER","password":"YOUR_PASS"}' | python3 -m json.tool
```

**Test 2: Sync PON + ONU**

```bash
TOKEN="<paste-token-dari-login>"

curl -s -X POST http://localhost:8000/api/v1/olts/1/sync-pons \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

**Test 3: List ONU**

```bash
curl -s http://localhost:8000/api/v1/olts/1/onus \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

**Test 4: Provision ONU baru**

Gunakan contoh request di bagian [Contoh Request](#contoh-request), lalu cek ONU muncul di list `/olts/{id}/onus` dan status PPPoE-nya sudah connected.

---

## Struktur Project

```
xqcd/
│
├── backend/
│   ├── app.py                    FastAPI + scheduler + seed
│   ├── olt_client.py             Client Telnet/SSH ZTE ZXAN
│   ├── zxan_parser.py            Parser running-config + generator config ONU
│   ├── snmp_client.py            SNMP client (opsional)
│   ├── alerting.py               Auto-generate + auto-resolve alert
│   ├── models.py                 SQLAlchemy models
│   ├── schemas.py                Pydantic schemas
│   ├── auth.py                   JWT + password hashing + audit
│   ├── database.py               SQLAlchemy engine + session
│   ├── config.py                 Settings dari .env
│   ├── requirements.txt          Python dependencies
│   ├── .env.example              Template konfigurasi
│   ├── routers/
│   │   ├── auth.py               Login, /me
│   │   ├── olts.py               CRUD OLT
│   │   ├── monitoring.py         Status, PON, ONU, metrics
│   │   ├── sync.py               Sync CPU/PON/ONU/optical/PPPoE
│   │   ├── onu.py                Provision / reboot / hapus ONU
│   │   ├── ports.py              Enable/disable port PON & uplink
│   │   ├── config.py             Running-config & VLAN
│   │   ├── alerts.py             Alert management
│   │   └── users.py              User + audit log
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

`send_config_set` sering timeout di ZXAN. Semua command dikirim satu per satu pakai `send_command_timing` dengan `read_timeout` cukup.

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

Backend simpan UTC. API return timestamp dengan `+ "Z"` supaya browser auto-convert ke WIB.

### 7. Huawei HG8145V5 tidak bisa provisioning via OLT ZTE

OMCI ZTE proprietary → Huawei tidak kenal ME-nya. Command yang gagal:

- `pppoe 1 nat enable user ...`
- `firewall enable level low anti-hack disable`
- `security-mgmt 1 state enable ...`
- `wan 1 service internet host 1`

**Solusi:** ganti ke ONU ZTE F609 (100% kompatibel).

---

## Troubleshooting

### "Pattern not detected" dari Netmiko

**Solusi:** Pakai `send_command_timing` per command (sudah dilakukan di `olt_client.py`).

### Provisioning sukses tapi ONU tidak ada di OLT

**Solusi:** Cek `zxan_parser.py` — pastikan ada `configure terminal` di awal + `end` + `write` di akhir.

### Alert tidak muncul

**Solusi:** Cek threshold di `.env` (atau `config.py`). Cek scheduler `[AUTO-SYNC]` di log.

### VLAN tidak muncul di OLT

**Solusi:** Cek `routers/config.py` — command harus pakai `vlan database` → `vlan N name X` → `exit`.

### Distance ONU masih angka lama

**Solusi:** Klik ☁️ Sync ulang.

### OLT tidak bisa diakses

**Solusi:** Cek telnet `IP:port` bisa dijangkau. Cek kredensial di `.env`. Cek enable password.

### Login gagal terus

**Solusi:** Cek apakah user sudah ada di DB. Reset DB (hapus `backend/data/*.db`) akan seed ulang user default.

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
# Real-time (development)
uvicorn app:app --reload --host 0.0.0.0 --port 8000 | grep AUTO-SYNC

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
- Auto-sync background tiap 60 detik + chart history
- Wizard provisioning ONU 4 langkah dengan verifikasi otomatis
- Reboot dan hapus ONU (OLT + DB) dengan verifikasi
- Enable/disable PON port dan uplink port
- Manajemen VLAN (tambah/hapus) dengan verifikasi ke OLT
- Alert system auto-generate + auto-resolve (`onu_offline`, `pppoe_down`, `optical_low`, `cpu_high`, `mem_high`)
- Audit log untuk semua aksi user
- JWT authentication, password hashing, role-based privilege

---

## Lisensi

MIT — lihat file [LICENSE](LICENSE).

## Kontribusi

Pull request welcome. Untuk perubahan besar, buka issue dulu untuk diskusi.
