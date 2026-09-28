# ZTE OLT C320 Manager

Aplikasi web untuk **monitoring & provisioning OLT ZTE C320**, dibuat untuk NOC ISP skala kecil.
Dibangun dengan **FastAPI + SQLite + Netmiko (Telnet) + vanilla JS**.

![Version](https://img.shields.io/badge/version-1.1.0-blue)
![Python](https://img.shields.io/badge/python-3.10+-green)
![FastAPI](https://img.shields.io/badge/fastapi-0.118+-red)
![License](https://img.shields.io/badge/license-MIT-yellow)

Dengan aplikasi ini kamu bisa memantau kondisi OLT dan ONU pelanggan, mendaftarkan (provision)
ONU baru, mengelola VLAN dan port, serta menerima alert otomatis — semuanya dari browser,
tanpa mengetik perintah CLI OLT satu per satu.

```
Browser (UI)  ⇄  FastAPI (backend)  ⇄  Telnet  ⇄  OLT ZTE C320  ⇄  ONU pelanggan
                       │
                       └── SQLite (user, alert, event, cache)
```

## Mulai Cepat

```bash
git clone https://github.com/kamaell111/xqcd.git
cd xqcd
cp backend/.env.example backend/.env    # lalu isi SECRET_KEY dan SEED_ADMIN_PASSWORD
./run.sh
```

Buka **http://localhost:8002**, login dengan `admin`, lalu tambahkan OLT lewat menu **OLT**.
Penjelasan lengkap tiap langkah ada di bagian [Cara Pakai](#cara-pakai-4-langkah).

## Daftar Isi

- [Fitur](#fitur)
- [Istilah Singkat](#istilah-singkat)
- [Arsitektur: Telnet-first](#arsitektur-telnet-first)
- [Requirement](#requirement)
- [Cara Pakai (4 Langkah)](#cara-pakai-4-langkah)
- [Konfigurasi Lanjutan](#konfigurasi-lanjutan)
- [Ganti Koneksi (LAN ↔ Publik)](#ganti-koneksi-lan--publik)
- [SNMP Trap (Opsional)](#snmp-trap-opsional)
- [Catatan Keamanan](#catatan-keamanan)
- [Struktur Folder](#struktur-folder)
- [Troubleshooting](#troubleshooting)
- [Teknologi](#teknologi)
- [Development](#development)
- [Lisensi](#lisensi)

---

## Fitur

**Monitoring (real-time dari OLT via Telnet):**
- CPU / Memory / Uptime OLT
- 16 PON port (status up/down)
- Daftar ONU (SN, nama, VLAN, PPPoE)
- Optical RX/TX per ONU (bulk per-PON)
- Distance + online duration
- Status internet (PPPoE connected/disconnected)
- Auto-sync background (default 30 detik)
- Deteksi ONU belum terdaftar (uncfg)
- Traffic chart: PON, Uplink, dan per-ONU

**Manajemen ONU:**
- Provision ONU (async job — respons < 1 detik)
- SN picker dari ONU belum terdaftar
- Preflight validation (SN duplikat, ONU ID bentrok, GEMPORT, service-port)
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
- **Multivers (super admin)**: pemilik semua OLT, bisa mengelola admin lain

**Async Job System:**
- Semua operasi write ke OLT memakai async job
- Job card floating dengan progress bar
- Two-phase commit untuk operasi destructive (perubahan baru permanen setelah di-**Commit**)
- Circuit breaker per OLT (kalau OLT bermasalah, request tidak menumpuk)

**Global Search:**
- Cari ONU, VLAN, PON Port, dan Interface sekaligus

---

## Istilah Singkat

Untuk kamu yang belum familiar dengan dunia OLT/GPON:

| Istilah | Artinya |
|---|---|
| **OLT** | Optical Line Terminal — perangkat utama di sisi penyedia layanan (di sini: ZTE C320) |
| **ONU** | Perangkat di sisi pelanggan (modem/ONT fiber) |
| **PON port** | Port di OLT tempat kabel fiber ke pelanggan tersambung (C320: 16 port) |
| **SN** | Serial Number ONU, dipakai untuk mendaftarkan ONU ke OLT |
| **uncfg** | ONU yang sudah terdeteksi OLT tapi belum didaftarkan |
| **GEMPORT / service-port** | Jalur data logis antara OLT dan ONU, serta pemetaannya ke VLAN |
| **PPPoE** | Metode login internet pelanggan (username/password) |
| **LOS** | Loss of Signal — sinyal optik dari ONU hilang (biasanya kabel putus/kotor) |
| **DyingGasp** | Sinyal terakhir ONU saat listrik pelanggan mati |
| **Multivers** | Istilah aplikasi untuk super admin yang memiliki semua OLT |

---

## Arsitektur: Telnet-first

Aplikasi ini mengambil semua data dan mengirim semua perintah ke OLT lewat **Telnet** (CLI ZXAN).
SNMP tidak wajib. Alasannya:

1. SNMP OLT sering tidak bisa diakses via IP publik (NAT forwarding terbatas)
2. Semua data yang dibutuhkan tersedia via CLI ZXAN dengan perintah **bulk per-PON**
3. Cache agresif + polling bertingkat = beban OLT ringan walau via publik
4. Clone-and-run konsisten: cukup buka port Telnet, selesai

**Data yang diambil via Telnet:**

| Data | Perintah | Sifat |
|---|---|---|
| CPU/Mem/Uptime | `show processor`, `show system-group` | Per-OLT |
| Status ONU | `show gpon onu state` | Bulk global |
| SN + Type ONU | `show gpon onu baseinfo gpon-olt_x/x/x` | Bulk per-PON |
| Optical Rx | `show pon power onu-rx gpon-olt_x/x/x` | Bulk per-PON |
| Optical Tx | `show pon power onu-tx gpon-olt_x/x/x` | Bulk per-PON |
| Traffic PON | `show interface gpon-olt_x/x/x` | Per-PON |
| Traffic Uplink | `show interface gei_/xgei_...` | Per-port |
| Traffic ONU | `show interface gpon-onu_x:x` | Per-ONU (batch) |
| Detail ONU | `show gpon onu detail-info gpon-onu_x:x` | Per-ONU |

SNMP tetap didukung sebagai jalur **opsional**: kalau OLT bisa dijangkau via SNMP dan
mengembalikan data valid, dipakai; kalau tidak, aplikasi langsung memakai Telnet.

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
git clone https://github.com/kamaell111/xqcd.git
cd xqcd
```

**Atau Download ZIP** — extract, lalu masuk ke foldernya (misalnya `xqcd-main`).
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
| `SEED_ADMIN_PASSWORD` | `admin123` | Password admin pertama |

> **Catatan:** variabel `SEED_*` hanya dipakai saat database **pertama kali dibuat**.
> Setelah database ada, ubah data OLT lewat aplikasi (menu **OLT**).
> Data OLT ditambahkan sendiri setelah login (lihat langkah 4).

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
- Meng-install dependencies (hanya sekali — run berikutnya dilewati)
- Menjalankan uvicorn di port **8002**

Kalau script bertanya **"Sudah edit .env? (y/n)"**, jawab **`y`** kalau sudah diedit (langkah 2).
Jawab `n` untuk memakai template default. Kalau terlanjur menjawab `n`, edit `backend/.env`
lalu jalankan `./run.sh` lagi.

### 4. Buka Aplikasi

Browser: **http://localhost:8002**

**Login default:**

| User | Password | Role |
|---|---|---|
| `admin` | `admin123` | **Multivers** (super admin) |
| `zte` | `zte123` | Operator (privilege 10) |
| `viewer` | `viewer123` | Viewer (privilege 5) |

> **Catatan:** user `admin` otomatis menjadi **Multivers** — punya akses penuh, bisa mengelola
> admin lain, dan bisa melihat semua OLT. Password `admin` mengikuti `SEED_ADMIN_PASSWORD` di `.env`.

**Langkah berikutnya setelah login:**
1. Menu **OLT** → **Tambah OLT** → isi IP, port, user, dan password OLT kamu (plus enable password kalau OLT memakainya)
2. Klik **Test Connection** → harus muncul "Telnet Connected"
3. Klik **Submit**
4. Tunggu 30–60 detik → sync otomatis berjalan → ONU muncul

⚠️ **Ganti semua password default setelah login pertama!**

---

## Konfigurasi Lanjutan

Semua pengaturan ada di `backend/.env`. Restart uvicorn setelah mengubahnya.

```bash
# ============ Scheduler ============
SNMP_POLL_INTERVAL=60              # interval sync CPU/Mem (detik)
TELNET_LOCK_TIMEOUT_S=5            # timeout lock untuk endpoint user-facing

# ============ Cache TTL (detik) ============
# Kecilkan = data lebih fresh, tapi beban OLT lebih besar
# Perbesar = hemat beban, tapi data bisa stale
PPPOE_CACHE_TTL_S=60               # status PPPoE
DETAIL_CACHE_TTL_S=600             # detail ONU (SN, distance)
OPTICAL_CACHE_TTL_S=120            # optical per-PON

# ============ Alert thresholds ============
ALERT_CPU_WARNING=85.0
ALERT_MEM_WARNING=90.0
ALERT_OPTICAL_RX_WARNING=-25.0     # dBm
ALERT_OPTICAL_RX_CRITICAL=-28.0    # dBm
ALERT_AUTO_RESOLVE=true
ALERT_DYING_GASP=false             # true = DyingGasp ikut dicatat sebagai alert

# ============ Proteksi VLAN manajemen ============
MANAGEMENT_VLAN=0                  # isi kalau OLT punya VLAN manajemen (tidak bisa dihapus dari UI)

# ============ SNMP Trap (opsional) ============
ENABLE_SNMP_TRAP=false             # default OFF — clone-and-run tanpa setup
```

---

## Ganti Koneksi (LAN ↔ Publik)

Kalau berpindah lokasi (misalnya kantor ↔ rumah), gunakan `switch-network.sh`. Script ini membaca
IP dari **environment variable**, jadi tidak ada IP asli yang tersimpan di source code.

**Setup (sekali saja, simpan di `~/.bashrc`):**

```bash
export OFFICE_OLT_IP=192.168.1.100
export OFFICE_OLT_PORT=23
export HOME_OLT_IP=203.0.113.10
export HOME_OLT_PORT=779
```

**Pakai:**

```bash
cd backend
./switch-network.sh office   # preset LAN
./switch-network.sh home     # preset publik
```

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
olt = db.query(OLT).first()        # kalau punya lebih dari satu OLT, sesuaikan filter-nya
olt.ip_address = "192.168.1.100"   # ganti dengan IP OLT yang baru
olt.port = 23                      # ganti dengan port yang baru
db.commit()
print(f"OLT: {olt.ip_address}:{olt.port}")
db.close()
PYEOF
```

---

## SNMP Trap (Opsional)

Untuk notifikasi real-time dari OLT:

1. Set `ENABLE_SNMP_TRAP=true` di `backend/.env`.
2. Arahkan UDP 162 ke port receiver aplikasi (Linux):
   ```bash
   sudo iptables -t nat -A PREROUTING -p udp --dport 162 -j REDIRECT --to-port 1620
   ```
   Rule ini hilang saat reboot — simpan dengan `iptables-persistent` / `netfilter-persistent`.
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
- **Telnet tidak terenkripsi.** Akses OLT lewat LAN atau VPN; jangan buka port Telnet OLT langsung ke internet tanpa proteksi.
- Jangan buka port aplikasi (8002) langsung ke internet. Gunakan VPN, atau reverse proxy dengan HTTPS.
- Kalau memakai SNMP: jangan pakai community `public`/`private`, dan batasi dengan ACL.

---

## Struktur Folder

```
xqcd/
├── backend/
│   ├── app.py                  FastAPI + scheduler + startup sync
│   ├── olt_client.py           Telnet client (Netmiko) + connection cache
│   ├── olt_manager.py          Lock per-OLT (RLock) + job store + circuit breaker
│   ├── olt_snmp.py             SNMP client (opsional, fallback)
│   ├── zxan_parser.py          Parser CLI ZXAN (config, traffic, optical)
│   ├── traffic_poller.py       Poll traffic PON + Uplink + ONU via Telnet
│   ├── vendor_detect.py        Deteksi vendor dari SN prefix
│   ├── alerting.py             Auto-generate + auto-resolve alert
│   ├── auth.py                 JWT + password hash + token_version
│   ├── models.py               SQLAlchemy models
│   ├── schemas.py              Pydantic schemas
│   ├── database.py             SQLAlchemy engine + WAL
│   ├── config.py               Settings (env var)
│   ├── backup.py               Backup database otomatis (6 jam)
│   ├── retention.py            Hapus data lama
│   ├── event_log.py            Log event ONU
│   ├── trap_receiver.py        SNMP Trap receiver (opsional)
│   ├── trap_sync.py            Light sync via trap
│   ├── recovery_tracker.py     Notifikasi recovery
│   ├── tenancy.py              Owner context (Multivers vs admin)
│   ├── requirements.txt        Dependencies
│   ├── .env.example            Template konfigurasi
│   ├── switch-network.sh       Switch LAN ↔ publik (via env var)
│   ├── data/                   SQLite DB + backup
│   └── routers/                10 router (auth, olts, monitoring, onu, dll)
├── frontend/
│   ├── index.html              UI
│   ├── app.js                  Logic JS
│   └── style.css               Dark theme
├── test/                       Script pengujian
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

Kalau muncul `Address already in use`, port 8002 sedang dipakai proses lain:

```bash
ss -tlnp | grep 8002
# atau
lsof -i :8002
```

### Tidak bisa login

- Pastikan username & password benar. Password `admin` mengikuti `SEED_ADMIN_PASSWORD` **saat database pertama dibuat** — mengubah `.env` sesudahnya tidak mengubah password yang sudah tersimpan.
- Muncul pesan **"Sesi berakhir"**: token login tidak valid atau kedaluwarsa, cukup login ulang. Ini juga terjadi kalau `SECRET_KEY` diubah, atau password/role user tersebut baru saja diganti.
- Kalau benar-benar lupa password, lihat bagian di bawah.

### Lupa password admin

Jalan terakhir: reset database. ⚠️ **Semua data (ONU, alert, user, riwayat) akan hilang.**
Hentikan uvicorn dulu, lalu:

```bash
cp -r backend/data backend/data.bak     # backup dulu
rm -f backend/data/*.db*
./run.sh                                 # database dibuat & di-seed ulang
```

Login dengan `SEED_ADMIN_USERNAME` / `SEED_ADMIN_PASSWORD` dari `.env`.

### OLT tidak connect (status "offline")

- Cek ping: `ping <IP_OLT>`
- Cek port Telnet: `telnet <IP_OLT> <PORT>`
- Cek kredensial — nilai `SEED_*` hanya terbaca saat database pertama dibuat.
  Kalau database sudah ada, ubah IP/port lewat menu **OLT**.
- Cek VLAN manajemen di OLT (kadang OLT hanya bisa diakses dari VLAN tertentu)
- Cek firewall / NAT forwarding (kalau akses via publik)

### ONU tidak muncul setelah provisioning

- Tunggu 30 detik (satu siklus scheduler)
- Atau klik tombol **Refresh** di halaman ONU
- Cek status job di Console browser (F12) atau `/api/v1/jobs`
- Cek log terminal untuk `[SYNC]` atau `[SYNC-PONS]`

### Alert spam (banyak notifikasi palsu)

- Pastikan `ALERT_DYING_GASP=false` (DyingGasp tidak dicatat sebagai alert)
- Naikkan `SNMP_POLL_INTERVAL` (misalnya 60 → 120 detik)

### Chip kuning "Config belum disimpan" terus muncul

- Chip ini muncul setelah perubahan **destructive** (hapus VLAN/ONU, disable port, edit port).
- Klik chip → **Commit** untuk menyimpan permanen ke OLT.
- Kalau OLT reboot sebelum di-commit, perubahan tersebut hilang (safety net).

### Traffic chart kosong

- Tunggu 2–3 siklus (butuh **minimal 2 sample** untuk menghitung delta rate)
- Untuk scope ONU: scheduler berjalan tiap 60 detik, chart terisi setelah 2–3 menit
- Refresh browser (`Ctrl+Shift+R`) setelah update frontend

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
uvicorn app:app --reload --host 0.0.0.0 --port 8002
```

`--host 0.0.0.0` membuka akses dari jaringan. Pakai `127.0.0.1` kalau hanya untuk lokal.

**Tips:**
- Dengan `--reload`, uvicorn restart otomatis saat file backend berubah.
- Cek syntax cepat: `python3 -m py_compile nama_file.py`
- Hard refresh browser (`Ctrl+Shift+R`) setelah mengedit frontend.
- Pakai branch Git untuk eksperimen.

---

## Lisensi

MIT — lihat file [LICENSE](LICENSE).

## Kontribusi

Pull request welcome. Untuk perubahan besar, buka issue dulu.
