# ZTE OLT C320 Manager

Aplikasi web untuk **monitoring & provisioning OLT ZTE C320**, dibuat untuk NOC ISP skala kecil.
Dibangun dengan **FastAPI + SQLite + SNMP (pysnmp) + Netmiko (Telnet) + vanilla JS**.

![Version](https://img.shields.io/badge/version-1.1.0-blue)
![Python](https://img.shields.io/badge/python-3.10%2B-green)
![FastAPI](https://img.shields.io/badge/fastapi-0.118%2B-red)
![License](https://img.shields.io/badge/license-MIT-yellow)
![CI](https://github.com/kamaell111/xqcd/actions/workflows/ci.yml/badge.svg)

Dengan aplikasi ini kamu bisa memantau kondisi OLT dan ONU pelanggan, mendaftarkan (provision)
ONU baru, mengelola VLAN dan port, serta menerima alert otomatis — semuanya dari browser,
tanpa mengetik perintah CLI OLT satu per satu.

```
                         ┌─── SNMP (pysnmp) ────┐
Browser (UI)  ⇄  FastAPI ┤                       ├──  OLT ZTE C320  ⇄  ONU pelanggan
                         └─── Telnet (netmiko) ──┘
                                    │
                                    └── SQLite (user, alert, event, cache)
```

SNMP dipakai sebagai jalur utama untuk **baca** data monitoring (cepat & ringan), Telnet dipakai
untuk semua operasi **tulis** dan sebagai fallback baca. Detail lengkap di bagian
[Arsitektur: Hybrid SNMP + Telnet](#arsitektur-hybrid-snmp--telnet).

## Mulai Cepat

**Dengan Docker (direkomendasikan):**

```bash
git clone https://github.com/kamaell111/xqcd.git
cd xqcd
cp backend/.env.example backend/.env
nano backend/.env    # isi SECRET_KEY + kredensial OLT
docker compose up -d
```

Buka **<http://localhost:8002>**, login dengan `admin`, lalu tambahkan OLT lewat menu **OLT**.

**Tanpa Docker?** Pakai `./run.sh` (butuh Python 3.10+) — lihat opsi B di bagian
[Cara Pakai](#cara-pakai-4-langkah).

## Daftar Isi

- [Fitur](#fitur)
- [Istilah Singkat](#istilah-singkat)
- [Arsitektur: Hybrid SNMP + Telnet](#arsitektur-hybrid-snmp--telnet)
- [SNMP Support Matrix](#snmp-support-matrix)
- [Requirement](#requirement)
- [Cara Pakai (4 Langkah)](#cara-pakai-4-langkah)
- [Konfigurasi Lanjutan](#konfigurasi-lanjutan)
- [Ganti Koneksi (LAN ↔ Publik)](#ganti-koneksi-lan--publik)
- [SNMP Trap (Opsional)](#snmp-trap-opsional)
- [Catatan Keamanan](#catatan-keamanan)
- [Struktur Folder](#struktur-folder)
- [Troubleshooting](#troubleshooting)
- [Teknologi](#teknologi)
- [Development & Testing](#development--testing)
- [Lisensi](#lisensi)

---

## Fitur

**Monitoring (real-time, SNMP-first dengan fallback Telnet):**

- CPU / Memory / Uptime OLT
- 16 PON port (status up/down, real-time via SNMP)
- Daftar ONU (SN, nama, VLAN, PPPoE) — instan via SNMP
- Optical RX/TX per ONU — instan via SNMP
- Distance + online duration
- Status internet (PPPoE connected/disconnected) — tetap via Telnet (tidak ada di MIB OLT)
- Auto-sync background (default 30 detik)
- Deteksi ONU belum terdaftar (uncfg)
- Traffic chart: PON, Uplink, dan per-ONU — via SNMP, **12–106× lebih cepat** dibanding Telnet

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

- Semua operasi write ke OLT memakai async job (tetap lewat Telnet)
- Job card floating dengan progress bar
- Two-phase commit untuk operasi destructive (perubahan baru permanen setelah di-**Commit**)
- Circuit breaker per OLT (kalau OLT bermasalah, request tidak menumpuk)

**Global Search:**

- Cari ONU, VLAN, PON Port, dan Interface sekaligus

---

## Istilah Singkat

Untuk kamu yang belum familiar dengan dunia OLT/GPON:

| Istilah                    | Artinya                                                                              |
| --------------------------- | ------------------------------------------------------------------------------------ |
| **OLT**                    | Optical Line Terminal — perangkat utama di sisi penyedia layanan (di sini: ZTE C320) |
| **ONU**                    | Perangkat di sisi pelanggan (modem/ONT fiber)                                        |
| **PON port**               | Port di OLT tempat kabel fiber ke pelanggan tersambung (C320: 16 port)               |
| **SN**                     | Serial Number ONU, dipakai untuk mendaftarkan ONU ke OLT                             |
| **uncfg**                  | ONU yang sudah terdeteksi OLT tapi belum didaftarkan                                 |
| **GEMPORT / service-port** | Jalur data logis antara OLT dan ONU, serta pemetaannya ke VLAN                       |
| **PPPoE**                  | Metode login internet pelanggan (username/password)                                  |
| **LOS**                    | Loss of Signal — sinyal optik dari ONU hilang (biasanya kabel putus/kotor)           |
| **DyingGasp**              | Sinyal terakhir ONU saat listrik pelanggan mati                                      |
| **SNMP**                   | Protokol monitoring standar; di sini dipakai lewat `pysnmp` untuk baca data cepat    |
| **OID**                    | Object Identifier — "alamat" satu data spesifik di dalam SNMP (mis. `sysUpTime`)     |
| **Multivers**              | Istilah aplikasi untuk super admin yang memiliki semua OLT                           |

---

## Arsitektur: Hybrid SNMP + Telnet

Aplikasi ini memakai **dua jalur komunikasi** ke OLT, dipilih sesuai kebutuhan:

- **SNMP (pysnmp)** — jalur utama untuk **BACA** data monitoring (ONU list, optical RX/TX,
  status PON, traffic, CPU/Memory). Satu SNMP walk bisa mengambil ratusan nilai sekaligus,
  jauh lebih cepat dan ringan dibanding Telnet yang harus membuka sesi CLI penuh lalu
  parsing teks per-command.
- **Telnet (netmiko / CLI ZXAN)** — jalur untuk semua operasi **TULIS** (provision/delete/reboot
  ONU, VLAN & port CRUD), karena ZTE C320 tidak menyediakan SNMP SET yang aman untuk perubahan
  config. Telnet juga dipakai sebagai **fallback otomatis** kalau SNMP OLT tidak bisa dijangkau
  (NAT publik tidak forward UDP 161) atau datanya memang tidak ada di MIB vendor
  (running-config, status PPPoE remote, temperature).

**Kenapa SNMP jadi prioritas baca:**

1. **Jauh lebih cepat** — traffic port 106× lebih cepat (0.28s vs 30s via Telnet), traffic ONU
   12× lebih cepat (0.2s vs 2.4s).
2. Satu walk SNMP mengambil banyak OID sekaligus, vs Telnet yang parsing teks CLI satu-satu.
3. Lebih ringan ke OLT — SNMP get/walk vs sesi Telnet penuh (login, enable, parsing output).
4. Tetap aman: kalau SNMP OLT down/unreachable, aplikasi otomatis jatuh ke Telnet tanpa
   intervensi manual.

7 dari 9 jenis data monitoring utama kini diambil via SNMP (lihat tabel lengkap di
[SNMP Support Matrix](#snmp-support-matrix)); 2 sisanya tetap hybrid dengan Telnet sebagai
fallback/pelengkap.

**Tetap via Telnet (tidak ada / belum ada di SNMP):**

- `show running-config` dan `show gpon onu uncfg` (di-cache 30–300 detik)
- Status PPPoE remote, temperature OLT
- Semua operasi **tulis**: provision/delete/reboot ONU, VLAN & port CRUD

Tabel OID lengkap + alasan per-fitur ada di bagian [SNMP Support Matrix](#snmp-support-matrix)
di bawah.

---

## SNMP Support Matrix

| Fitur                             | OID / Method                      | Sumber | Speed vs Telnet                     |
| ---------------------------------- | ----------------------------------- | ------ | -------------------------------------- |
| ONU list (nama, SN, tipe, status, distance) | `3902.1012.3.28.x`        | SNMP   | Instan                                 |
| Optical RX/TX ONU                 | `3902.1012.3.50.12.1.1.10/.14`     | SNMP   | Instan                                 |
| Traffic port (uplink/PON)         | `ifHCIn/OutOctets` (Counter64)     | SNMP   | **106×** lebih cepat (0.28s vs 30s)    |
| Traffic ONU                       | `3902.1015.1010.5.56.1.1.2/.3`     | SNMP   | **12×** lebih cepat (0.2s vs 2.4s)     |
| PON port status (up/down)         | `ifOperStatus` + `ifAdminStatus`   | SNMP   | Real-time                              |
| ONU status detail (LOS/online/reason) | `3902.1082.500.10.2.3.8.1.4`   | SNMP   | Alert LOS lewat SNMP                   |
| ONU last online/offline timestamp | OID `...8.1.5/.6`                  | SNMP   | Terverifikasi 4×                       |
| CPU/Memory per-slot               | `3902.1082.10.1.2.4.1.11/.9`       | SNMP   | Fallback Telnet kalau gagal             |
| Board info (tipe, serial, firmware)| `3902.1082.10.1.2.4.1.4/14/19/27` | SNMP   | Per-slot                               |
| OLT model/uptime                  | `sysDescr` / `sysUpTime`           | SNMP   | Standar (IF-MIB)                       |
| Running-config, uncfg ONU         | `show running-config/uncfg`        | Telnet | Di-cache 30–300 detik                  |
| PPPoE remote, temperature OLT     | —                                   | Telnet | Tidak ada di MIB OLT                   |
| Provision/delete/reboot ONU, VLAN CRUD | CLI ZXAN                      | Telnet | SNMP SET tidak dipakai demi keamanan   |

> Kalau SNMP gagal terhubung (mis. community salah, atau OLT diakses lewat NAT publik tanpa
> UDP 161 di-forward), aplikasi otomatis jatuh ke Telnet untuk data yang masih bisa diambil
> lewat CLI — tidak ada downtime monitoring, hanya lebih lambat.

---

## Requirement

| Komponen | Minimum                                                                         |
| -------- | -------------------------------------------------------------------------------- |
| OS       | Linux (Ubuntu/Debian/Arch) atau macOS                                            |
| Docker   | Docker 24+ & Docker Compose v2 — **kalau pakai jalur Docker (direkomendasikan)** |
| Python   | 3.10+ — **kalau jalan tanpa Docker** (`./run.sh`)                                |
| RAM      | 512 MB                                                                            |
| Storage  | 500 MB                                                                            |
| Akses    | Telnet ke OLT (wajib — tulis + fallback baca)                                    |
| Akses    | SNMP/UDP 161 ke OLT (opsional tapi disarankan — jalur baca utama, jauh lebih cepat)|

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

| Variabel              | Contoh                             | Keterangan              |
| --------------------- | ----------------------------------- | ------------------------ |
| `SECRET_KEY`          | *(hasil generate, lihat di bawah)*  | Kunci penanda JWT token  |
| `SEED_ADMIN_PASSWORD` | `admin123`                          | Password admin pertama   |

> **Catatan:** variabel `SEED_*` hanya dipakai saat database **pertama kali dibuat**.
> Setelah database ada, ubah data OLT lewat aplikasi (menu **OLT**).
> Data OLT (termasuk community SNMP) ditambahkan sendiri setelah login (lihat langkah 4).

**Generate `SECRET_KEY`:**

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

Copy hasilnya, paste ke `backend/.env` di baris `SECRET_KEY=`.
Jangan diganti-ganti setelah dipakai — semua sesi login yang aktif akan tidak valid.

### 3. Jalankan

**Opsi A — Docker (direkomendasikan):** `docker compose up -d` — otomatis build image,
jalankan uvicorn, dan mount `backend/data/` sebagai volume persisten. Log: `docker compose logs -f`.

**Opsi B — Python lokal:**

```bash
./run.sh
```

Script ini otomatis menyalin `.env.example` → `.env` (kalau belum ada), membuat virtual
environment, meng-install dependencies (sekali saja), lalu menjalankan uvicorn di port **8002**.
Kalau script bertanya **"Sudah edit .env? (y/n)"**, jawab **`y`** kalau sudah diedit (langkah 2).

### 4. Buka Aplikasi

Browser: **<http://localhost:8002>**

**Login default:**

| User     | Password    | Role                        |
| -------- | ----------- | ---------------------------- |
| `admin`  | `admin123`  | **Multivers** (super admin)  |
| `zte`    | `zte123`    | Operator (privilege 10)      |
| `viewer` | `viewer123` | Viewer (privilege 5)         |

> **Catatan:** user `admin` otomatis menjadi **Multivers** — punya akses penuh, bisa mengelola
> admin lain, dan bisa melihat semua OLT. Password `admin` mengikuti `SEED_ADMIN_PASSWORD` di `.env`.

**Langkah berikutnya setelah login:**

1. Menu **OLT** → **Tambah OLT** → isi IP, port Telnet, user, password, enable password, serta
   **community SNMP** (default `public` — ganti sesuai OLT kamu, lihat [Catatan Keamanan](#catatan-keamanan))
2. Klik **Test Connection** → harus muncul "Telnet Connected" (dan "SNMP Connected" kalau terisi)
3. Klik **Submit**
4. Tunggu 30–60 detik → sync otomatis berjalan → ONU muncul

⚠️ **Ganti semua password default setelah login pertama!**

---

## Konfigurasi Lanjutan

Semua pengaturan ada di `backend/.env`. Restart uvicorn (atau `docker compose restart`) setelah
mengubahnya. Community/versi SNMP dikonfigurasi **per-OLT lewat menu OLT**, bukan lewat `.env`.

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

**Trap = hint, sync = source of truth.** Trap hanya memicu verifikasi ulang (via SNMP, fallback
Telnet), bukan langsung mengubah database. Ini terpisah dari jalur SNMP baca reguler yang dipakai
untuk monitoring — lihat [Arsitektur](#arsitektur-hybrid-snmp--telnet).

---

## Catatan Keamanan

- Ganti semua password default (aplikasi **dan** OLT) sebelum dipakai di jaringan produksi.
- Jangan commit `backend/.env` dan `backend/data/` ke Git — isinya `SECRET_KEY`, kredensial OLT,
  community SNMP, dan database.
- **Telnet tidak terenkripsi.** Akses OLT lewat LAN atau VPN; jangan buka port Telnet OLT
  langsung ke internet tanpa proteksi.
- Jangan buka port aplikasi (8002) langsung ke internet. Gunakan VPN, atau reverse proxy
  dengan HTTPS.
- Kalau memakai SNMP: jangan pakai community `public`/`private`, dan batasi dengan ACL
  (SNMP v2c tidak terenkripsi, sama seperti Telnet).
- Untuk melaporkan vulnerability, lihat [SECURITY.md](SECURITY.md) — jangan buka lewat issue publik.

---

## Struktur Folder

```
xqcd/
├── .github/workflows/ci.yml        Lint + test + secret-scan + docker-build
├── .github/ISSUE_TEMPLATE/bug_report.md
├── .editorconfig, .dockerignore, .gitignore
├── Dockerfile, docker-compose.yml
├── SECURITY.md                     Cara report vulnerability
├── CONTRIBUTING.md                 Setup + alur PR
├── pyproject.toml                  Config ruff + pytest
├── backend/
│   ├── app.py                      FastAPI + scheduler + startup sync
│   ├── olt_client.py               Telnet client (Netmiko) + connection cache
│   ├── olt_manager.py              Lock per-OLT + job store + circuit breaker
│   ├── olt_snmp.py                 SNMP client (pysnmp, ~700 baris) — jalur baca utama
│   ├── traffic_poller.py           Poll traffic PON/Uplink/ONU (SNMP, fallback Telnet)
│   ├── zxan_parser.py              Parser CLI ZXAN (config, traffic, optical)
│   ├── vendor_detect.py, alerting.py, auth.py, tenancy.py
│   ├── models.py, schemas.py, database.py, config.py
│   ├── backup.py, retention.py, event_log.py
│   ├── trap_receiver.py, trap_sync.py, recovery_tracker.py   SNMP Trap (opsional)
│   ├── requirements.txt            Dependencies (+ pysnmp)
│   ├── .env.example, switch-network.sh
│   ├── data/                       SQLite DB + backup (+ .gitkeep)
│   ├── routers/                    19 router (auth, olts, monitoring, onu, dll)
│   ├── tests/                      11 file — 227 test (pytest)
│   └── scripts/                    Debug/scratch script manual
├── frontend/                       index.html, app.js, style.css (dark theme)
├── logs/
├── .venv/                          Dibuat otomatis oleh run.sh (jalur non-Docker)
├── run.sh                          Launcher (clone → run, tanpa Docker)
├── LICENSE                         MIT
└── README.md                       Dokumen ini
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

Pakai Docker? Cek dulu `docker compose ps` dan `docker compose logs -f`.

### Tidak bisa login

Password `admin` mengikuti `SEED_ADMIN_PASSWORD` **saat database pertama dibuat** — mengubah
`.env` sesudahnya tidak mengubah password yang sudah tersimpan. Pesan **"Sesi berakhir"** berarti
token tidak valid/kedaluwarsa (login ulang), atau `SECRET_KEY`/password/role baru saja berubah.
Benar-benar lupa password? Lihat bagian di bawah.

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

Cek `ping <IP_OLT>` dan `telnet <IP_OLT> <PORT>`. Cek kredensial lewat menu **OLT** (nilai
`SEED_*` hanya terbaca saat database pertama dibuat). Cek VLAN manajemen OLT, dan firewall/NAT
forwarding kalau akses via publik — Telnet butuh TCP, SNMP butuh UDP 161.

### Monitoring lambat / terus fallback ke Telnet

Cek community SNMP di menu **OLT** (default `public` sering mismatch) dan pastikan UDP 161
bisa dijangkau dari server aplikasi. Kalau SNMP memang tidak bisa dipakai (mis. NAT publik
tanpa forwarding), aplikasi tetap jalan normal lewat Telnet — hanya lebih lambat, bukan error.

### ONU tidak muncul setelah provisioning

Tunggu 30 detik (satu siklus scheduler) atau klik **Refresh** di halaman ONU. Cek status job
di Console browser (F12) / `/api/v1/jobs`, atau log terminal untuk `[SYNC]`/`[SYNC-PONS]`.

### Alert spam (banyak notifikasi palsu)

Pastikan `ALERT_DYING_GASP=false`, dan naikkan `SNMP_POLL_INTERVAL` (misalnya 60 → 120 detik).

### Chip kuning "Config belum disimpan" terus muncul

Muncul setelah perubahan **destructive** (hapus VLAN/ONU, disable port, edit port). Klik chip →
**Commit** untuk menyimpan permanen ke OLT — kalau OLT reboot sebelum di-commit, perubahan hilang
(safety net).

### Traffic chart kosong

Butuh **minimal 2 sample** untuk menghitung delta rate (scope ONU: scheduler tiap 60 detik,
terisi setelah 2–3 menit). Coba refresh browser (`Ctrl+Shift+R`) setelah update frontend.

### Backup & restore database

- Backup otomatis tiap 6 jam di `backend/data/backups/`, rotasi 7 file terakhir.
- Restore (uvicorn harus **berhenti**):

```bash
cp backend/data/backups/<file_backup>.db backend/data/zte_olt_manager.db
rm -f backend/data/zte_olt_manager.db-wal backend/data/zte_olt_manager.db-shm
```

File `-wal`/`-shm` (SQLite WAL mode) harus dihapus supaya tidak tercampur dengan database restore.

---

## Teknologi

| Layer         | Tech                                              |
| ------------- | -------------------------------------------------- |
| Backend       | Python + FastAPI                                   |
| Database      | SQLite (WAL mode)                                   |
| OLT Client    | Netmiko (`zte_zxros_telnet`) + pysnmp 7.x (SNMP)    |
| Frontend      | Vanilla JS + Chart.js 4.4                           |
| Auth          | JWT + pbkdf2_sha256                                 |
| Scheduler     | APScheduler                                         |
| Testing       | pytest (227 test)                                   |
| CI/CD         | GitHub Actions (lint + test + secret-scan + docker-build) |
| Code quality  | ruff                                                 |

**Dua cara jalan:** Docker Compose (terisolasi, direkomendasikan) atau clone-and-run lokal tanpa
Docker (`./run.sh`). Tidak butuh Redis, tidak butuh cloud.

---

## Development & Testing

Setup manual (tanpa Docker, tanpa `run.sh`):

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

**Menjalankan test (227 test, di `backend/tests/`):**

```bash
cd backend
pytest                              # semua test
pytest tests/test_olt_snmp.py -v    # satu file spesifik
pytest --cov=. --cov-report=term    # dengan coverage (kalau pytest-cov terpasang)
```

**Lint & format (ruff, config di `pyproject.toml`):**

```bash
ruff check .
ruff format .
```

**CI:** setiap push/PR ke `main` otomatis menjalankan lint + test + secret-scan + docker-build
lewat GitHub Actions (`.github/workflows/ci.yml`). Status terbaru ada di badge CI paling atas,
atau tab [Actions](https://github.com/kamaell111/xqcd/actions).

**Tips:**

- Dengan `--reload`, uvicorn restart otomatis saat file backend berubah.
- Cek syntax cepat: `python3 -m py_compile nama_file.py`
- Hard refresh browser (`Ctrl+Shift+R`) setelah mengedit frontend.
- Pakai branch Git untuk eksperimen.
- Lihat [CONTRIBUTING.md](CONTRIBUTING.md) untuk alur kontribusi lengkap (setup, konvensi commit, PR).

---

## Lisensi

MIT — lihat file [LICENSE](https://github.com/kamaell111/xqcd/blob/main/LICENSE).

## Kontribusi

Pull request welcome. Untuk perubahan besar, buka issue dulu. Detail setup dan alur PR ada di
[CONTRIBUTING.md](CONTRIBUTING.md).
