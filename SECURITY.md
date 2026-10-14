# Security Policy

OLT Manager mengelola koneksi langsung ke perangkat OLT (network infra). Kerentanan di
sini berpotensi berdampak ke jaringan produksi — tolong laporkan secara bertanggung jawab.

## Supported Versions

Hanya branch `main` yang didukung. Tidak ada maintenance untuk tag/versi lama.

## Melaporkan Kerentanan

**Jangan buat public issue untuk masalah keamanan.**

Gunakan salah satu:
- GitHub Security Advisories → tab *Security* → *Report a vulnerability*

Sertakan: deskripsi, langkah reproduksi, dan dampak potensial (contoh: akses OLT tanpa
autentikasi, injeksi command ke perangkat via netmiko/SNMP, exposure kredensial).

## Scope

**Termasuk:** auth bypass, credential/secret exposure, command injection ke OLT,
path traversal, dependency dengan CVE aktif yang reachable dari kode ini.

**Di luar scope:** kerentanan pada firmware/hardware OLT itu sendiri, social engineering,
DoS murni terhadap instance milik sendiri.

## Response

Proyek ini di-maintain best-effort (bukan tim keamanan dedicated). Target acknowledge
laporan dalam ~5 hari kerja. Mohon coordinated disclosure — tunggu fix tersedia sebelum
publikasi publik.
