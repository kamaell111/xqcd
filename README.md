# ZTE OLT C320 Manager

Aplikasi web untuk monitoring & provisioning OLT ZTE C320, dibuat untuk NOC ISP skala kecil.
Dibangun dengan FastAPI + SQLite + Netmiko (Telnet) + vanilla JS.

![Version](https://img.shields.io/badge/version-1.0.0-blue)
![Python](https://img.shields.io/badge/python-3.10+-green)
![FastAPI](https://img.shields.io/badge/fastapi-0.118+-red)
![License](https://img.shields.io/badge/license-MIT-yellow)

---

## Fitur

**Monitoring (real-time dari OLT):**
- CPU / Memory / Uptime
- 16 PON port (status up/down)
- Daftar ONU (SN, nama, VLAN, PPPoE)
- Optical RX/TX per ONU
- Distance + online duration
- Status internet (PPPoE connected/disconnected)
- Auto-sync background (default 30 detik)
- Deteksi ONU belum terdaftar (uncfg)

**Manajemen ONU:**
- Provision ONU (async job — respons < 1 detik)
- SN picker dari ONU belum terdaftar
- Preflight validation (cek SN duplikat, ONU ID bentrok, GEMPORT, service-port)
- Auto-suggest GEMPORT / service-port / ONU ID
- Hapus + reboot ONU
- Halaman detail per ONU + timeline event
- Mode routing: PPPoE (ZTE) / Bridge (Huawei/FiberHome)

**Manajemen VLAN & Port:**
- CRUD VLAN dengan dependency check
- Guard VLAN manajemen (tidak bisa dihapus)
- Enable/disable PON + uplink port
- Edit mode VLAN uplink (access/trunk/hybrid)

**Alert System:**
- Auto-generate + auto-resolve
- Kategori: `onu_offline`, `onu_los`, `pppoe_down`, `optical_low`, `cpu_high`, `mem_high`
- Notifikasi recovery
- Ack + resolve + bulk delete
- DyingGasp suppression (kurangi alert noise)

**User Management:**
- Multi-user dengan role: admin / operator / field_tech / viewer
- Role → privilege otomatis (15 / 10 / 8 / 5)
- Edit user, reset password, ganti password sendiri
- JWT security: token otomatis tidak valid saat password/role berubah
- Guard admin terakhir (tidak bisa dihapus/dinonaktifkan)

**Async Job System:**
- Semua operasi write ke OLT memakai async job
- Job card floating dengan progress bar
- Two-phase commit untuk operasi destructive
- Circuit breaker per OLT

**Global Search:**
- Cari ONU, VLAN, PON Port, dan Interface sekaligus

---

## Requirement

| Komponen | Minimum |
|---|---|
| OS | Linux (Ubuntu/Debian/Arch) atau macOS |
| Python | 3.10+ |
| RAM | 512 MB |
| Storage | 500 MB |
| Akses | Telnet ke OLT (LAN, atau publik — untuk publik disarankan lewat VPN) |

**Opsional** — SNMP Trap (default OFF): port UDP 162 + iptables redirect.

---

## Cara Pakai (4 Langkah)

### 1. Download / Clone

```bash
# Cara 1: clone dari GitHub
git clone https://github.com/kamaell111/xqcd.git
cd xqcd
```

**Cara 2: Download ZIP** — extract, lalu masuk ke foldernya (misalnya `xqcd-main`).
File ZIP tidak menyimpan izin eksekusi, jadi jalankan dulu:

```bash
chmod +x run.sh backend/switch-network.sh
```

### 2. Konfigurasi `.env`

```bash
cp backend/.env.example backend/.env
nano backend/.env
```

Yang **WAJIB** diisi:

| Variabel | Contoh | Keterangan |
|---|---|---|
| `SECRET_KEY` | *(hasil generate, lihat di bawah)* | Kunci penanda JWT token |
| `SEED_OLT_IP` | `192.168.1.100` | IP OLT kamu |
| `SEED_OLT_PORT` | `23` | Port Telnet OLT (ganti jika OLT diakses lewat port forwarding) |
| `SEED_OLT_USERNAME` | `zte` | Username OLT |
| `SEED_OLT_PASSWORD` | `zte` | Password OLT |
| `SEED_OLT_ENABLE_PASSWORD` | `zxr10` | Enable password OLT |

> **Catatan:** variabel `SEED_*` hanya dipakai saat database **pertama kali dibuat**.
> Kalau database sudah ada, ubah data OLT lewat aplikasi (menu OLT) atau lihat bagian
> [Ganti Koneksi](#ganti-koneksi-lan--publik).

**Generate `SECRET_KEY`:**

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

Copy hasilnya, paste ke `backend/.env` di baris `SECRET_KEY=`.
Jangan diganti-ganti setelah dipakai — semua sesi login yang aktif akan tidak valid.

### 3. Jalankan

```bash
./run.sh
```

Script ini otomatis:
- Menyalin `.env.example` → `.env` (kalau belum ada)
- Membuat virtual environment (`.venv`)
- Meng-install dependencies
- Menjalankan uvicorn di port **8001**

Kalau script bertanya **"Sudah edit .env? (y/n)"**, jawab **`y`** (kamu sudah mengeditnya di langkah 2).
Kalau terlanjur menjawab `n`, edit `backend/.env` lalu jalankan `./run.sh` lagi.

### 4. Buka Aplikasi

Browser: **http://localhost:8001**

**Login default:**

| User | Password | Role |
|---|---|---|
| `admin` | `admin123` | Admin (privilege 15) |
| `zte` | `zte123` | Operator (privilege 10) |
| `viewer` | `viewer123` | Viewer (privilege 5) |

⚠️ **Ganti semua password default setelah login pertama!**

---

## Ganti Koneksi (LAN ↔ Publik)

Kalau berpindah lokasi (misalnya kantor ↔ rumah), ganti tujuan koneksi OLT dengan script:

```bash
cd backend
./switch-network.sh office   # preset LAN
./switch-network.sh home     # preset publik
```

IP dan port tiap preset didefinisikan di dalam `switch-network.sh` — sesuaikan dengan jaringanmu.
Setelah itu **restart uvicorn**.

⚠️ Script hanya mengubah `.env`. Karena data OLT sudah tersimpan di database, IP/port di
**database** juga perlu diperbarui.

**Cara termudah:** login → menu **OLT** → edit IP/port.

**Alternatif via terminal** (jalankan dari folder `backend`, virtual environment aktif):

```bash
cd backend
source ../.venv/bin/activate
python3 << 'PYEOF'
from database import SessionLocal
from models import OLT

db = SessionLocal()
olt = db.query(OLT).first()
olt.ip_address = "192.168.1.100"   # ganti dengan IP OLT yang baru
olt.port = 23                      # ganti dengan port yang baru
db.commit()
print(f"OLT: {olt.ip_address}:{olt.port}")
db.close()
PYEOF
```

---

## Konfigurasi Lanjutan

Semua pengaturan ada di `backend/.env`. Restart uvicorn setelah mengubahnya.

```bash
# Scheduler — interval polling OLT (detik)
SNMP_POLL_INTERVAL=30         # 15 untuk LAN, 30 untuk publik

# SNMP Trap (opsional, default OFF)
ENABLE_SNMP_TRAP=false

# Alert thresholds
ALERT_CPU_WARNING=85.0
ALERT_MEM_WARNING=90.0
ALERT_OPTICAL_RX_WARNING=-25.0
ALERT_OPTICAL_RX_CRITICAL=-28.0
ALERT_AUTO_RESOLVE=true
ALERT_DYING_GASP=false        # true = DyingGasp ikut dicatat sebagai alert

# Proteksi VLAN manajemen (tidak bisa dihapus dari UI)
MANAGEMENT_VLAN=0             # isi kalau OLT punya VLAN manajemen
```

---

## SNMP Trap (Opsional)

Untuk notifikasi real-time dari OLT:

1. Set `ENABLE_SNMP_TRAP=true` di `backend/.env`.
2. Arahkan UDP 162 ke port receiver aplikasi (Linux):
   ```bash
   sudo iptables -t nat -A PREROUTING -p udp --dport 162 -j REDIRECT --to-port 1620
   ```
   Rule ini hilang saat reboot — simpan dengan `iptables-persistent` / `netfilter-persistent` jika perlu.
3. Konfigurasi di OLT:
   ```
   snmp-server host <IP_SERVER> version 2c <community> enable NOTIFICATIONS
   target-addr-name NMS_SERVER isnmsserver udp-port 162
   trap-report-compatibility v20
   ```
4. Restart uvicorn.

**Trap = hint, sync = source of truth.** Trap hanya memicu verifikasi ulang via Telnet,
bukan langsung mengubah database.

---

## Catatan Keamanan

- Ganti semua password default (aplikasi **dan** OLT) sebelum dipakai di jaringan produksi.
- Jangan commit `backend/.env` dan `backend/data/` ke Git — isinya `SECRET_KEY`, kredensial OLT, dan database.
- Telnet tidak terenkripsi. Akses OLT lewat LAN atau VPN; jangan buka port Telnet OLT langsung ke internet.
- Jangan buka port aplikasi (8001) langsung ke internet. Gunakan VPN, atau reverse proxy dengan HTTPS.

---

## Struktur Folder

```
xqcd/
├── backend/
│   ├── app.py                  FastAPI + scheduler + startup sync
│   ├── olt_client.py           Telnet client (Netmiko)
│   ├── olt_manager.py          Lock per-OLT + job store + circuit breaker
│   ├── zxan_parser.py          Parser running-config + generator config
│   ├── vendor_detect.py        Deteksi vendor dari SN prefix
│   ├── alerting.py             Auto-generate + auto-resolve alert
│   ├── auth.py                 JWT + password hash + token_version
│   ├── models.py               SQLAlchemy models
│   ├── schemas.py              Pydantic schemas
│   ├── database.py             SQLAlchemy engine + WAL
│   ├── config.py               Settings
│   ├── backup.py               Backup database otomatis
│   ├── retention.py            Hapus data lama
│   ├── event_log.py            Log event ONU
│   ├── trap_receiver.py        SNMP Trap receiver (opsional)
│   ├── trap_sync.py            Light sync via trap
│   ├── recovery_tracker.py     Notifikasi recovery
│   ├── requirements.txt        Dependencies
│   ├── .env.example            Template konfigurasi
│   ├── switch-network.sh       Switch LAN ↔ publik
│   ├── data/                   SQLite DB + backup
│   └── routers/                10 router (auth, olts, monitoring, onu, dll)
├── frontend/
│   ├── index.html              UI
│   ├── app.js                  Logic JS
│   └── style.css               Dark theme
├── test/
│   ├── test_netmiko_connection.py
│   └── test_snmp_trap.py
├── .venv/                      Dibuat otomatis oleh run.sh
├── run.sh                      Launcher (clone → run)
├── LICENSE                     MIT
└── README.md                   Dokumen ini
```

---

## Troubleshooting

### Uvicorn tidak mau jalan

```bash
python3 --version              # minimal 3.10
cd backend
source ../.venv/bin/activate
pip install -r requirements.txt
```

Kalau muncul error `Address already in use`, port 8001 sedang dipakai proses lain:

```bash
lsof -i :8001
```

### Tidak bisa login

**Muncul pesan "Sesi berakhir"** — token login tidak valid atau kedaluwarsa. Login ulang.
Ini juga terjadi kalau `SECRET_KEY` diubah, atau password/role user tersebut baru saja diganti.

**Lupa password admin** — sebagai jalan terakhir, reset database. ⚠️ Semua data (ONU, alert,
user, riwayat) akan hilang. Hentikan uvicorn dulu, lalu:

```bash
cp -r backend/data backend/data.bak     # backup dulu
rm -f backend/data/*.db*
# Jalankan ./run.sh lagi — database dibuat & di-seed ulang
```

Setelah di-seed ulang, login dengan user default (atau password dari `SEED_ADMIN_PASSWORD` di `.env`).

### OLT tidak connect (status "offline")

- Cek ping: `ping <IP_OLT>`
- Cek port: `telnet <IP_OLT> <PORT>`
- Cek kredensial di `backend/.env` — ingat, nilai `SEED_*` hanya terbaca saat database pertama dibuat.
  Kalau database sudah ada, cek/ubah IP dan port di menu **OLT**.
- Cek VLAN manajemen di OLT (kadang OLT hanya bisa diakses dari VLAN tertentu)

### ONU tidak muncul setelah provisioning

- Tunggu 30 detik (satu siklus scheduler)
- Atau klik tombol **Refresh** di halaman ONU
- Cek status job di Console browser (F12) atau `/api/v1/jobs`

### Alert spam (banyak notifikasi palsu)

- Pastikan `ALERT_DYING_GASP=false` (DyingGasp tidak dicatat sebagai alert)
- Naikkan `SNMP_POLL_INTERVAL` (30 → 60 detik)

### Chip kuning "Config belum disimpan" terus muncul

- Chip ini muncul setelah perubahan **destructive** (hapus VLAN/ONU, disable port, edit port).
- Klik chip → **Commit** untuk menyimpan permanen ke OLT.
- Kalau dibiarkan dan OLT reboot sebelum di-commit, perubahan tersebut akan hilang (safety net).

### Backup & restore database

- Backup otomatis tiap 6 jam di `backend/data/backups/`, rotasi 7 file terakhir.
- Restore (uvicorn harus **berhenti**):
  ```bash
  cp backend/data/backups/<file_backup>.db backend/data/zte_olt_manager.db
  rm -f backend/data/zte_olt_manager.db-wal backend/data/zte_olt_manager.db-shm
  ```
  File `-wal` dan `-shm` (SQLite WAL mode) harus dihapus supaya tidak tercampur dengan database hasil restore.

---

## Teknologi

| Layer | Tech |
|---|---|
| Backend | Python + FastAPI |
| Database | SQLite (WAL mode) |
| OLT Client | Netmiko (`zte_zxros_telnet`) |
| Frontend | Vanilla JS + Chart.js 4.4 |
| Auth | JWT + pbkdf2_sha256 |
| Scheduler | APScheduler |

**Clone-and-run:** tidak butuh Docker, tidak butuh Redis, tidak butuh cloud.

---

## Development

Setup manual (tanpa `run.sh`):

```bash
python3 -m venv .venv
source .venv/bin/activate
cd backend
pip install -r requirements.txt
cp .env.example .env
nano .env                       # edit konfigurasi
uvicorn app:app --reload --host 0.0.0.0 --port 8001
```

`--host 0.0.0.0` membuka akses dari jaringan. Pakai `127.0.0.1` kalau hanya untuk lokal.

**Tips:**
- Dengan `--reload`, uvicorn restart otomatis saat file backend berubah.
- Cek syntax cepat: `python3 -m py_compile nama_file.py`
- Hard refresh browser (`Ctrl+Shift+R`) setelah mengedit frontend.
- Pakai branch Git untuk eksperimen, jadi tidak perlu membuat file `.bak`.

---

## Lisensi

MIT — lihat file [LICENSE](LICENSE).

## Kontribusi

Pull request welcome. Untuk perubahan besar, buka issue dulu.
